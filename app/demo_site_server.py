"""Owned test-page process; readiness endpoint exposes identity only, no business rows."""
import argparse,os
import uvicorn
from app.merchant_test_portal import create_merchant_test_app


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,required=True);args=parser.parse_args()
    app=create_merchant_test_app(username='demo_reader',password='demo-pass-2026')
    @app.get('/restock-demo-health')
    def ready():return {'status':'ok','service':'restock-merchant-demo','instance':os.environ['RESTOCK_DEMO_INSTANCE']}
    uvicorn.run(app,host='127.0.0.1',port=args.port,log_level='warning')


if __name__=='__main__':main()
