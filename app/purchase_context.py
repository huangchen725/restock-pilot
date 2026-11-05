"""Validate source-visible purchase identities against a complete CSV snapshot."""
from datetime import datetime,date,timedelta,timezone
from app.models import PurchaseContext


def attach(snapshot,context,source_as_of):
    if context is None:return snapshot
    context=PurchaseContext.model_validate(context).model_copy(deep=True)
    raw=context.timezone_offset
    offset=timedelta(hours=int(raw[1:3]),minutes=int(raw[4:])) * (-1 if raw[0]=='-' else 1)
    at=datetime.fromisoformat(source_as_of.replace('Z','+00:00'))
    if at.tzinfo is None or at.utcoffset()!=offset:
        raise ValueError('采购日期时区与源时间不一致')
    as_of=at.date()
    inventory={x['sku']:x for x in snapshot.inventory}
    for row in context.orders:
        if row.sku not in inventory:raise ValueError('采购上下文包含未授权 SKU')
        if date.fromisoformat(row.created_date)>as_of:raise ValueError('采购创建日期晚于源数据时间')
    for sku,inv in inventory.items():
        incoming=[x for x in context.orders if x.sku==sku and x.status in ('ordered','in_transit')]
        if sum(x.quantity for x in incoming)!=int(inv['inbound_qty']):
            raise ValueError('采购明细与在途汇总数量不一致')
        dates={x.expected_date for x in incoming}
        if len(dates)>1:raise ValueError('多批采购到货日期不同，不能表达为单一 ETA')
        if dates:
            eta=(date.fromisoformat(next(iter(dates)))-as_of).days
            if eta<0 or str(eta)!=inv['inbound_eta']:
                raise ValueError('采购到货日期与在途 ETA 不一致')
    return snapshot.model_copy(update={'purchase_context':context})
