"""Customer-scoped real-video editing, review, and private artifact routes."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, File, Form, Header, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, Response

from opc_style.domain.errors import DomainRuleError
from opc_style.services.video_outputs import (
    MAX_VIDEO_BYTES,
    VideoOutput,
    VideoOutputStore,
)

router = APIRouter(prefix="/v1/video-outputs", tags=["video-outputs"])
local_router = APIRouter(
    prefix="/local/style-suite/video-output", tags=["local-video-output"]
)

VIDEO_OUTPUT_PAGE = """<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>真实讲解视频</title><style>
body{font-family:system-ui,"PingFang SC",sans-serif;background:#f7f4ef;color:#29231f;margin:0}.wrap{max-width:780px;margin:auto;padding:40px 20px}form,.result{background:#fff;border:1px solid #e5ddd2;border-radius:18px;padding:24px;box-shadow:0 18px 55px #42322614}label{display:block;margin:14px 0}input,textarea,button{width:100%;box-sizing:border-box;padding:11px;border-radius:9px;border:1px solid #d8cfc4}textarea{min-height:130px}button{background:#29231f;color:#fff;font-weight:700;cursor:pointer}.notice{padding:12px;background:#fff6df;border:1px solid #ecd8aa;border-radius:10px}.result{margin-top:18px}.result video,.result img{max-width:100%;border-radius:12px;margin-top:10px}[hidden]{display:none!important}
</style></head><body><main class="wrap"><p><a href="/local/style-suite/output-center">← 返回内容输出中心</a></p><h1>真实讲解视频</h1>
<p class="notice">只处理你主动上传并确认可编辑的真实视频；字幕、封面和成片均需人工审核，不做人像或声音克隆，不会自动发布。</p>
<form id="video-form"><label>客户ID<input name="customer_id" required placeholder="UUID"></label><label>标题<input name="title" required maxlength="120"></label><label>真实视频<input name="source" type="file" accept="video/mp4,video/quicktime" required></label><label>可用片段开始秒数<input name="clip_start_seconds" type="number" min="0" step="0.1" value="0"></label><label>可用片段结束秒数（留空代表到结尾）<input name="clip_end_seconds" type="number" min="0" step="0.1"></label><label>画面包装<select name="package_style"><option value="original">保留原比例</option><option value="catalog-light">浅色商品卡 4:5</option><option value="catalog-dark">深色商品卡 4:5</option></select></label><label><input name="denoise_audio" type="checkbox" value="true" style="width:auto"> 轻度降噪</label><label><input name="normalize_audio" type="checkbox" value="true" checked style="width:auto"> 音量统一</label><label>字幕（SRT，可留空）<textarea name="subtitles" placeholder="1&#10;00:00:00,000 --> 00:00:02,000&#10;商品说明"></textarea></label><label><input name="permission" type="checkbox" required style="width:auto"> 我确认拥有此视频并授权编辑</label><button>生成待审视频</button></form>
<section id="result" class="result" hidden><strong id="state"></strong><video id="video" controls></video><img id="cover" alt="自动提取的视频封面"><p>结果不可直接发布，需在接口完成客户审核。</p></section>
<p id="status" aria-live="polite"></p></main><script>
const form=document.getElementById('video-form'),statusNode=document.getElementById('status'),result=document.getElementById('result');let urls=[];form.onsubmit=async event=>{event.preventDefault();urls.forEach(URL.revokeObjectURL);urls=[];result.hidden=true;statusNode.textContent='正在本机处理真实视频…';const data=new FormData(form),customer=data.get('customer_id');data.delete('customer_id');data.set('transform_permission_confirmed','true');data.set('idempotency_key',`browser-video-${Date.now()}`);try{const response=await fetch('/v1/video-outputs',{method:'POST',headers:{'X-Customer-Id':customer},body:data}),payload=await response.json();if(!response.ok)throw Error(payload.error?.code||`HTTP ${response.status}`);document.getElementById('state').textContent='已生成，等待人工审核';for(const artifact of payload.artifacts){if(!['video','cover'].includes(artifact.kind))continue;const media=await fetch(artifact.content_url,{headers:{'X-Customer-Id':customer}}),url=URL.createObjectURL(await media.blob());urls.push(url);document.getElementById(artifact.kind).src=url}result.hidden=false;statusNode.textContent='字幕、封面和成片均已保存；尚未发布。'}catch(error){statusNode.textContent='处理失败：'+error.message}};
</script></body></html>"""


@local_router.get("", response_class=HTMLResponse)
def video_output_page() -> HTMLResponse:
    return HTMLResponse(VIDEO_OUTPUT_PAGE, headers={"Cache-Control": "no-store"})


def _store(request: Request) -> VideoOutputStore:
    configured = getattr(request.app.state, "video_output_store", None)
    if not isinstance(configured, VideoOutputStore):
        raise DomainRuleError("video_output_store_unconfigured")
    return configured


@router.post("", status_code=status.HTTP_201_CREATED, response_model=None)
async def create_video_output(
    request: Request,
    source: Annotated[UploadFile, File()],
    title: Annotated[str, Form(min_length=1, max_length=120)],
    idempotency_key: Annotated[str, Form(min_length=1, max_length=200)],
    transform_permission_confirmed: Annotated[bool, Form()],
    subtitles: Annotated[str, Form()] = "",
    clip_start_seconds: Annotated[float, Form(ge=0, le=86_400)] = 0.0,
    clip_end_seconds: Annotated[float | None, Form(gt=0, le=86_400)] = None,
    denoise_audio: Annotated[bool, Form()] = False,
    normalize_audio: Annotated[bool, Form()] = True,
    package_style: Annotated[
        Literal["original", "catalog-light", "catalog-dark"], Form()
    ] = "original",
    x_customer_id: Annotated[UUID | None, Header(alias="X-Customer-Id")] = None,
) -> dict[str, object] | JSONResponse:
    if x_customer_id is None:
        return _error("customer_id_required", 422)
    if not transform_permission_confirmed:
        return _error("video_transform_permission_required", 409)
    content = await source.read(MAX_VIDEO_BYTES + 1)
    try:
        result, created = _store(request).create(
            customer_id=x_customer_id,
            source=content,
            source_mime_type=source.content_type or "",
            subtitles=subtitles,
            title=title,
            idempotency_key=idempotency_key,
            clip_start_seconds=clip_start_seconds,
            clip_end_seconds=clip_end_seconds,
            denoise_audio=denoise_audio,
            normalize_audio=normalize_audio,
            package_style=package_style,
        )
    except DomainRuleError as error:
        return _domain_error(error)
    payload = _payload(result)
    payload["created"] = created
    if not created:
        return JSONResponse(payload, status_code=status.HTTP_200_OK)
    return payload


@router.get("/{output_id}", response_model=None)
def get_video_output(
    output_id: UUID,
    request: Request,
    x_customer_id: Annotated[UUID, Header(alias="X-Customer-Id")],
) -> dict[str, object] | JSONResponse:
    try:
        return _payload(_store(request).get(x_customer_id, output_id))
    except DomainRuleError as error:
        return _domain_error(error)


@router.post("/{output_id}/review", response_model=None)
def review_video_output(
    output_id: UUID,
    request: Request,
    decision: Annotated[Literal["approved", "rejected"], Form()],
    human_confirmation: Annotated[bool, Form()],
    x_customer_id: Annotated[UUID, Header(alias="X-Customer-Id")],
) -> dict[str, object] | JSONResponse:
    if not human_confirmation:
        return _error("video_human_confirmation_required", 409)
    try:
        return _payload(
            _store(request).review(x_customer_id, output_id, decision=decision)
        )
    except DomainRuleError as error:
        return _domain_error(error)


@router.get("/{output_id}/artifacts/{kind}")
def get_video_artifact(
    output_id: UUID,
    kind: Literal["video", "subtitles", "cover"],
    request: Request,
    x_customer_id: Annotated[UUID, Header(alias="X-Customer-Id")],
) -> Response:
    try:
        content, mime_type, digest = _store(request).content(
            x_customer_id, output_id, kind
        )
    except DomainRuleError:
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    return Response(
        content,
        media_type=mime_type,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Sha256": digest,
            "X-Content-Type-Options": "nosniff",
        },
    )


def _payload(record: VideoOutput) -> dict[str, object]:
    base = f"/v1/video-outputs/{record.output_id}/artifacts"
    return {
        "output_id": str(record.output_id),
        "title": record.title,
        "state": record.state,
        "source_sha256": record.source_sha256,
        "edit_plan": record.edit_plan,
        "pipeline_version": "real-video-edit-v1",
        "publishable": False,
        "requires_human_review": record.state == "human_review_required",
        "voice_cloning_used": False,
        "external_provider_calls": 0,
        "artifacts": [
            {
                "kind": artifact.kind,
                "content_sha256": artifact.content_sha256,
                "mime_type": artifact.mime_type,
                "size_bytes": artifact.size_bytes,
                "content_url": f"{base}/{artifact.kind}",
            }
            for artifact in record.artifacts
        ],
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def _domain_error(error: DomainRuleError) -> JSONResponse:
    status_code = 404 if error.code in {
        "video_output_not_found",
        "video_artifact_not_found",
    } else 409
    if error.code in {"video_processing_failed", "video_output_store_unconfigured"}:
        status_code = 503
    return _error(error.code, status_code)


def _error(code: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "retryable": status_code == 503}},
        status_code=status_code,
    )
