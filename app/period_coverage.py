"""Pure, independently testable store-cycle coverage and committed budget facts.

Input local lines must be validated against the existing order ledger by the
caller. Source facts come from the immutable collected snapshot. No I/O/model.
"""
from datetime import datetime,date,time,timedelta,timezone
from pydantic import BaseModel,ConfigDict,Field


class CommittedLine(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    system_id:str=Field(min_length=1,max_length=160)
    order_id:str=Field(min_length=1,max_length=160)
    sku:str=Field(min_length=1,max_length=100)
    supplier_id:str=Field(min_length=1,max_length=100)
    quantity:int=Field(gt=0)
    amount_cents:int=Field(gt=0)
    status:str
    created_at:str=Field(min_length=1,max_length=40)
    budget_at:str|None=None


def instant(value):
    try:
        at=datetime.fromisoformat(value.replace('Z','+00:00'))
        if at.tzinfo is None:raise ValueError()
        return at.astimezone(timezone.utc)
    except (TypeError,ValueError,AttributeError):
        raise ValueError('周期和采购时间必须包含明确时区') from None


def calculate(snapshot,local_lines,period_start,period_end,*,target_system_id,relationship,source_as_of):
    context=snapshot.purchase_context
    if context is None:raise ValueError('缺少完整采购上下文，不能把未知当作没有外部申请')
    if relationship not in ('same','separate','mapped'):
        raise ValueError('来源与执行目标的单据对应关系未确认')
    if (relationship=='same') != (context.system_id==target_system_id):
        raise ValueError('采购系统身份与确认的对应关系矛盾')
    start,end=instant(period_start),instant(period_end)
    if start>=end:raise ValueError('预算周期起止无效')
    asof=instant(source_as_of)
    offset=context.timezone_offset
    tz=timezone(timedelta(hours=int(offset[1:3]),minutes=int(offset[4:]))*(-1 if offset[0]=='-' else 1))
    local={};source={};skus={r['sku'] for r in snapshot.inventory}
    for raw in local_lines:
        row=CommittedLine.model_validate(raw)
        if row.status!='SUBMITTED' or row.system_id!=target_system_id or row.sku not in skus:
            raise ValueError('本地申请状态、系统或 SKU 不能可靠核验')
        instant(row.created_at)
        key=(row.system_id,row.order_id,row.sku)
        if key in local and local[key]!=row:raise ValueError('同一已创建单据的回读相互矛盾')
        local[key]=row
    for row in context.orders:
        if row.origin_system_id:
            if relationship!='mapped' or row.origin_system_id!=target_system_id:
                raise ValueError('采购映射不属于已确认的执行系统')
            key=(row.origin_system_id,row.origin_order_id,row.sku)
            if key not in local:raise ValueError('采购映射没有对应的已核验本地申请')
        else:key=(context.system_id,row.order_id,row.sku)
        if key in source:raise ValueError('多条来源记录映射为同一单据，不能重复抵扣')
        source[key]=row
    covered={};occupied=0;facts=[]
    for key in sorted(set(local)|set(source)):
        own=local.get(key);external=source.get(key)
        if own and external:
            if (own.quantity!=external.quantity or own.supplier_id!=external.supplier_id
                    or (external.amount_cents is not None and external.amount_cents!=own.amount_cents)):
                raise ValueError('申请与来源采购明细不一致，请先核对映射')
        if own and not external and relationship in ('same','mapped') and instant(own.created_at)<=asof:
            raise ValueError('完整来源批次缺少已有申请的对应记录，不能猜测是否已计入库存/在途')
        # A matched arrival/inbound is already represented in the snapshot;
        # requested lines and verified local-only commitments cover extra demand.
        quantity=(external.quantity if external.status=='requested' else 0) if external else own.quantity
        if quantity:covered[key[2]]=covered.get(key[2],0)+quantity
        charge=0
        if own:
            if start<=instant(own.budget_at or own.created_at)<end:charge=own.amount_cents
        else:
            day=datetime.combine(date.fromisoformat(external.created_date),time(),tzinfo=tz)
            next_day=day+timedelta(days=1)
            overlap=day<end and next_day>start
            if overlap and not(start<=day and next_day<=end):
                raise ValueError('来源只有创建日期，无法判定跨半日预算周期的归属')
            if overlap:
                if external.amount_cents is None:raise ValueError('当前周期外部采购缺少原单金额，不能用当前商品价格代填')
                charge=external.amount_cents
        occupied+=charge
        facts.append({'identity':list(key),'source':external.model_dump() if external else None,
                      'local':own.model_dump() if own else None,'extra_covered_qty':quantity,'charged_cents':charge})
    return {'period_start':start.isoformat(),'period_end':end.isoformat(),
            'source_system_id':context.system_id,'target_system_id':target_system_id,'relationship':relationship,
            'covered':covered,'occupied_cents':occupied,'orders':facts}
