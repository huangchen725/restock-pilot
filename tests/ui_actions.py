"""User actions shared across UI contracts; labels are asserted independently."""
from playwright.sync_api import expect

CHECKS=('数量按单个商品填写（例如按瓶，不按箱）','库存已减去留给现有订单的数量',
        '采购价按分填写，并包含税费和运费','到货时间填写还需几天')

def choose_entry(page,label):
    routes=page.get_by_role('radiogroup',name='资料获取方式',exact=True)
    if not routes.is_visible() and page.get_by_text('更换资料或准备新任务',exact=True).count():
        page.get_by_text('更换资料或准备新任务',exact=True).click()
    expect(routes).to_be_visible()
    page.get_by_text(label,exact=True).click()


def auto_entry(page):
    choose_entry(page,'自动采集网页')
    page.get_by_text('定期检查补货',exact=True).click()


def manual_source(page):
    page.get_by_text('只生成这次方案',exact=True).click()
