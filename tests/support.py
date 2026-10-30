"""Synchronous legacy regression client; approval emulates a reviewed current page."""
from fastapi.testclient import TestClient
from app import main


class SyncClient(TestClient):
    def __init__(self, app):
        super().__init__(app, headers={"Prefer":"wait", "X-Restock-Approval":main.APPROVAL_TOKEN})

    def post(self, url, **kwargs):
        if url.endswith("/approve") and "json" not in kwargs:
            state=self.get(url.removesuffix('/approve')).json()
            plan=state.get('latest_plan') or {}
            kwargs['json']={"version":plan.get('version',1),"plan_hash":state.get('latest_plan_hash') or '0'*64}
        return super().post(url, **kwargs)
