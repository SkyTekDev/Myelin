from fastapi import APIRouter, HTTPException, Request
from bridge.esrd_reference import EsrdReferenceRequest, EsrdReferenceResponse

router = APIRouter(prefix='/api/v1/esrd', tags=['ESRD'])


@router.get('/ready')
def ready(request: Request):
    processor = getattr(request.app.state, 'esrd_references', None)
    if processor is None:
        raise HTTPException(503, 'ESRD benchmark references are disabled or unavailable.')
    return processor.catalog.readiness()


@router.post('/references', response_model=EsrdReferenceResponse)
def references(payload: EsrdReferenceRequest, request: Request):
    processor = getattr(request.app.state, 'esrd_references', None)
    if processor is None:
        raise HTTPException(503, 'ESRD benchmark references are disabled or unavailable.')
    try:
        return processor.resolve(payload)
    except Exception:
        raise HTTPException(503, 'ESRD reference lookup is unavailable.') from None
