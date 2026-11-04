"""Human/backend collection endpoints; not exposed in the product Agent tool list."""
import secrets
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict

class CollectionEnabled(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool

from app import collection, db


def router(get_conn, get_root, get_token):
    api = APIRouter()

    def authorize(token):
        if not secrets.compare_digest(token, get_token()):
            raise HTTPException(403, '只有已配置的人工工作台或可信采集接入可以管理来源。')

    def invoke(function, *args):
        try:
            return function(*args)
        except collection.CollectionError as exc:
            raise HTTPException(exc.status, str(exc)) from None

    @api.post('/data-sources')
    def create(config: collection.SourceConfig, x_restock_approval: str = Header(default='')):
        authorize(x_restock_approval)
        sid = invoke(collection.create_source, get_conn(), config)
        return invoke(collection.source_info, get_conn(), sid, get_root())

    @api.get('/data-sources')
    def sources():
        return {'sources': [collection.source_info(get_conn(), r['source_id'], get_root()) for r in db.list_sources(get_conn())]}

    @api.get('/data-sources/{source_id}')
    def source(source_id: str):
        return invoke(collection.source_info, get_conn(), source_id, get_root())

    @api.put('/data-sources/{source_id}/settings')
    def settings(source_id: str, schedule: collection.Schedule, x_restock_approval: str = Header(default='')):
        authorize(x_restock_approval)
        invoke(collection.update_schedule, get_conn(), source_id, schedule)
        return invoke(collection.source_info, get_conn(), source_id, get_root())

    @api.post('/data-sources/{source_id}/observations')
    def observe(source_id: str, observation: collection.Observation, x_restock_approval: str = Header(default='')):
        authorize(x_restock_approval)
        result = invoke(collection.accept_observation, get_conn(), source_id, observation)
        if result['http_status'] != 200:
            raise HTTPException(result['http_status'], result['problems'])
        return result

    @api.post('/data-sources/{source_id}/publish')
    def publish(source_id: str, x_restock_approval: str = Header(default='')):
        authorize(x_restock_approval)
        return invoke(collection.publish, get_conn(), get_root(), source_id)

    @api.put('/data-sources/{source_id}/collection-enabled')
    def enabled(source_id: str, request: CollectionEnabled, x_restock_approval: str = Header(default='')):
        authorize(x_restock_approval)
        from app.collector import set_enabled
        invoke(set_enabled,get_conn(),source_id,request.enabled)
        return invoke(collection.source_info,get_conn(),source_id,get_root())

    @api.post('/data-sources/{source_id}/collect', status_code=202)
    def collect(source_id: str, x_restock_approval: str = Header(default='')):
        authorize(x_restock_approval)
        from app.collector import claim
        return {'run_id':invoke(claim,get_conn(),source_id),'status':'queued'}

    @api.get('/data-sources/{source_id}/collection-runs')
    def runs(source_id: str):
        invoke(collection.get_source,get_conn(),source_id)
        return {'runs':db.collection_runs(get_conn(),source_id)}

    @api.get('/data-sources/{source_id}/publications')
    def publications(source_id: str):
        invoke(collection.get_source, get_conn(), source_id)
        return {'publications': db.list_publications(get_conn(), source_id)}

    @api.get('/data-sources/{source_id}/publications/{dataset_id}/{filename}')
    def download(source_id: str, dataset_id: str, filename: str):
        content = invoke(collection.publication_bytes, get_conn(), get_root(), source_id, dataset_id, filename)
        media = 'application/json' if filename == 'manifest.json' else 'text/csv'
        return Response(content, media_type=media, headers={'Content-Disposition': f'attachment; filename="{filename}"'})

    return api
