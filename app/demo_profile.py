"""Explicit connection metadata for the supplied test page, never business data."""
DEMO_SKUS=['SKU-'+str(1000+i) for i in (1,2,3,4,5,6,8,9,10,12,13,15,16,17,18,20,22,24,25,26,27,29,30,31,32,33,34,36,37,39)]
MERCHANT_NAME='示例便利（模拟）有限公司'


def profile(url):
    return {'url':url,'name':'城南店演示资料','store_name':'城南店','warehouse_name':'城南店仓',
            'merchant_id':'S01','warehouse_id':'W01','merchant_name':MERCHANT_NAME,'sku_scope':DEMO_SKUS,
            'history_notice':'测试页面使用历史演示数据。仅此演示连接允许 31 天数据时效；正式店铺需另行约定。',
            'schedule':{'collect_every_seconds':1800,'save_every_seconds':3600,'check_every_seconds':600,'max_age_seconds':2678400}}
