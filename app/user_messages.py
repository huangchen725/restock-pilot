"""Translate boundary errors without hiding the original diagnostic evidence."""
import re

FIELDS={'date':'销售日期','sku':'商品编码','sold_qty':'销售数量','name':'商品名称',
        'category':'商品类别','available_qty':'可用库存','inbound_qty':'在途数量',
        'inbound_eta':'距到货天数','supplier_id':'供应商编码','unit_cost_cents':'单价（分）',
        'lead_days':'交货天数','pack_size':'每包件数','min_order_qty':'最低采购件数'}
FILES={'sales':'销售记录','inventory':'库存资料','suppliers':'供应商资料'}


def friendly_error(value):
    text=str(value)
    match=re.search(r'(sales|inventory|suppliers) row (\d+): invalid (\w+)',text)
    if match:
        name,row,field=match.groups()
        rule='请使用 YYYY-MM-DD 格式。' if field=='date' else '请填写符合要求的非负数；数量须为整数，单价和包装件数须大于零。'
        return f'{FILES[name]}第 {int(row)+1} 行（含表头）的“{FIELDS.get(field,field)}”无效。{rule}'
    match=re.search(r'(sales|inventory|suppliers) row (\d+): missing columns (.+)',text)
    if match:
        name,_,columns=match.groups()
        fields=', '.join(FIELDS.get(f,f) for f in re.findall(r"'([^']+)'",columns))
        return f'{FILES[name]}缺少列：{fields}。请下载标准模板，保持表头不变后填入资料。'
    match=re.search(r'(sales|inventory|suppliers) row (\d+): (\w+) is required',text)
    if match:
        name,row,field=match.groups()
        return f'{FILES[name]}第 {int(row)+1} 行的“{FIELDS.get(field,field)}”不能为空。'
    if 'missing days' in text:
        return '销售记录未覆盖完整的 28 个自然日。请补齐缺失日期；只有确认缺失代表零销量时，才可勾选允许补零。'
    if 'duplicate' in text:
        return '资料中存在重复记录。请检查同一商品、同一销售日期是否重复，库存与供应商表每个商品只保留一行。'
    if 'empty table' in text:
        return '资料表为空，请按标准模板填写数据后重新检查。'
    if any(x in text for x in ('ERR_CONNECTION','ConnectError','Connection refused','connect to','ConnectionError')):
        return '暂时无法连接采购后台。本次结果已保留；恢复服务后先点“重新核对申请”，不要另建任务重复采购。'
    if "Executable doesn't exist" in text or 'browserType.launch' in text:
        return '录单浏览器不可用，请停止服务，在启动窗口点击“安装 / 修复环境”，再返回本任务核对。'
    if 'unsupported requests' in text:
        return '这项要求暂不支持。可调整预算、覆盖天数、安全天数、优先类别或排除商品；请修改要求或使用参数表单。'
    if any(x in text for x in ('agent failed','Agent runtime failed','Reviser policy','Codex CLI','RESTOCK_CODEX_BIN')):
        return '方案助手暂时未能完成处理。请在启动窗口检查模型配置；未生成新的可批准方案，修正后可重试。'
    if 'timeout' in text.lower() or 'Timeout' in text:
        return '等待处理超时。若正在创建申请，请先重新核对已有单据，再决定是否恢复。'
    if any(x in text for x in ('mismatch','不一致','状态或单号无效')):
        return '回读信息与批准方案不一致，已停止新增申请。请核对对应单据，修正后台问题后重新查询。'
    if isinstance(value,list) and value and isinstance(value[0],dict):
        return '填写内容不符合要求，请检查必填项、预算和天数范围后再试。'
    if re.search('[\u4e00-\u9fff]',text): return text
    return '本次操作未完成，请检查资料或启动状态后重试。已有资料和记录会保留。'
