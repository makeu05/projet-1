import json
import os
import time
from decimal import Decimal
from typing import Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi import FastAPI, File, Form, Header, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

AWS_ENDPOINT_URL = os.getenv("AWS_ENDPOINT_URL") or None
AWS_REGION = os.getenv("AWS_DEFAULT_REGION", "eu-west-3")
S3_BUCKET = os.getenv("S3_BUCKET", "vidp-videos")
DDB_TABLE = os.getenv("DDB_TABLE", "vidp-metadata")
EDGE_NODE_TOKEN = os.getenv("EDGE_NODE_TOKEN", "dev-edge-token-change-me")

s3 = boto3.client(
    "s3",
    endpoint_url=AWS_ENDPOINT_URL,
    region_name=AWS_REGION,
    config=Config(s3={"addressing_style": "path"}),
)
dynamodb = boto3.resource("dynamodb", endpoint_url=AWS_ENDPOINT_URL, region_name=AWS_REGION)
table = dynamodb.Table(DDB_TABLE)

SEGMENTS_RECEIVED = Counter("vidp_segments_received_total", "Segments recus par l'API Cloud", ["mode", "resolution"])
SEGMENTS_DEDUP = Counter("vidp_segments_deduplicated_total", "Segments ignores car deja enregistres")
E2E_LATENCY = Histogram(
    "vidp_e2e_latency_seconds",
    "Latence evenement -> reception Cloud",
    ["mode"],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 180.0],
)

app = FastAPI(title="VidP-Edge Cloud Aggregator")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _clean_decimals(obj):
    if isinstance(obj, list):
        return [_clean_decimals(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _clean_decimals(v) for k, v in obj.items()}
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    return obj


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/api/segments")
async def ingest_segment(
    metadata: str = Form(...),
    video: Optional[UploadFile] = File(None),
    subtitles: Optional[UploadFile] = File(None),
    x_node_token: Optional[str] = Header(None),
):
    if x_node_token != EDGE_NODE_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid node token")

    try:
        meta = json.loads(metadata, parse_float=Decimal)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid metadata JSON")

    segment_id = meta.get("segment_id")
    if not segment_id:
        raise HTTPException(status_code=400, detail="Missing segment_id")

    now_ms = int(time.time() * 1000)
    event_ts_ms = int(meta.get("event_ts_ms", now_ms))
    mode = str(meta.get("mode", "unknown"))
    resolution = str(meta.get("resolution", "unknown"))

    video_key = None
    if video is not None:
        video_key = f"videos/{segment_id}.mp4"
        s3.upload_fileobj(video.file, S3_BUCKET, video_key, ExtraArgs={"ContentType": "video/mp4"})

    sub_key = None
    if subtitles is not None:
        sub_key = f"subtitles/{segment_id}.vtt"
        s3.upload_fileobj(subtitles.file, S3_BUCKET, sub_key, ExtraArgs={"ContentType": "text/vtt"})
    elif meta.get("subtitles_vtt"):
        sub_key = f"subtitles/{segment_id}.vtt"
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=sub_key,
            Body=str(meta["subtitles_vtt"]).encode("utf-8"),
            ContentType="text/vtt",
        )

    item = {
        **meta,
        "cloud_received_ts_ms": now_ms,
        "e2e_latency_ms": max(0, now_ms - event_ts_ms),
        "s3_video_key": video_key or "",
        "s3_subtitles_key": sub_key or "",
    }

    try:
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(segment_id)")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            SEGMENTS_DEDUP.inc()
            return {"status": "duplicate_ignored", "segment_id": segment_id}
        raise HTTPException(status_code=500, detail=str(e))

    SEGMENTS_RECEIVED.labels(mode=mode, resolution=resolution).inc()
    E2E_LATENCY.labels(mode=mode).observe(max(0.0, (now_ms - event_ts_ms) / 1000.0))
    return {"status": "stored", "segment_id": segment_id, "e2e_latency_ms": item["e2e_latency_ms"]}


@app.get("/api/segments")
def list_segments():
    raw_items = []
    resp = table.scan()
    raw_items.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        raw_items.extend(resp.get("Items", []))
    items = [_clean_decimals(i) for i in raw_items]
    items.sort(key=lambda x: x.get("cloud_received_ts_ms", 0), reverse=True)
    return {"items": items}


@app.get("/api/media/{key:path}")
def get_media(key: str):
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
    except ClientError:
        raise HTTPException(status_code=404, detail="Media not found")
    return StreamingResponse(obj["Body"], media_type=obj.get("ContentType", "application/octet-stream"))


@app.delete("/api/segments")
def delete_all_segments():
    resp = table.scan(ProjectionExpression="segment_id")
    deleted = 0
    for item in resp.get("Items", []):
        table.delete_item(Key={"segment_id": item["segment_id"]})
        deleted += 1
    return {"deleted": deleted}
