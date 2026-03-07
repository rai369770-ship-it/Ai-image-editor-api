import base64
import io
import os
import random
import asyncio
from typing import List

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI(
    title="Image Editor API",
    version="1.0.0",
    description="AI image editing service powered by MagicEraser.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class EditRequest(BaseModel):
    image: str = Field(..., description="Base64 encoded image content")
    prompt: str = Field(..., description="Editing instruction prompt")
    filename: str = Field("input.jpg", description="Original filename with extension")


class EditResponse(BaseModel):
    success: bool
    job_id: str
    image: str


class RootResponse(BaseModel):
    success: bool
    message: str


def generate_serial() -> str:
    return "".join(random.choice("0123456789abcdef") for _ in range(32))


def detect_content_type(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".png":
        return "image/png"
    return "image/jpeg"


async def upload_filename(client: httpx.AsyncClient, filename: str) -> dict:
    response = await client.post(
        "https://api.imgupscaler.ai/api/common/upload/upload-image",
        data={"file_name": filename},
        headers={
            "origin": "https://imgupscaler.ai",
            "referer": "https://imgupscaler.ai/",
        },
    )
    response.raise_for_status()
    return response.json()["result"]


async def upload_to_oss(client: httpx.AsyncClient, put_url: str, image_bytes: bytes, content_type: str) -> bool:
    response = await client.put(
        put_url,
        content=image_bytes,
        headers={
            "Content-Type": content_type,
            "Content-Length": str(len(image_bytes)),
        },
    )
    return response.status_code == 200


async def create_job(client: httpx.AsyncClient, image_url: str, prompt: str) -> str:
    response = await client.post(
        "https://api.magiceraser.org/api/magiceraser/v2/image-editor/create-job",
        data={
            "model_name": "magiceraser_v4",
            "original_image_url": image_url,
            "prompt": prompt,
            "ratio": "match_input_image",
            "output_format": "jpg",
        },
        headers={
            "product-code": "magiceraser",
            "product-serial": generate_serial(),
            "origin": "https://imgupscaler.ai",
            "referer": "https://imgupscaler.ai/",
        },
    )
    response.raise_for_status()
    return response.json()["result"]["job_id"]


async def check_job(client: httpx.AsyncClient, job_id: str) -> dict:
    response = await client.get(
        f"https://api.magiceraser.org/api/magiceraser/v1/ai-remove/get-job/{job_id}",
        headers={
            "origin": "https://imgupscaler.ai",
            "referer": "https://imgupscaler.ai/",
        },
    )
    response.raise_for_status()
    return response.json()


@app.get("/", response_model=RootResponse)
async def root():
    return RootResponse(
        success=True,
        message="Use /edit-image via POST method to edit an image.",
    )


@app.post("/edit-image", response_model=EditResponse)
async def edit_image(request: EditRequest):
    try:
        image_bytes = base64.b64decode(request.image, validate=True)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 encoded image.")

    if len(image_bytes) == 0:
        raise HTTPException(status_code=400, detail="Decoded image is empty.")

    content_type = detect_content_type(request.filename)

    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        try:
            upload_result = await upload_filename(client, request.filename)
        except Exception:
            raise HTTPException(status_code=502, detail="Failed to obtain upload URL.")

        put_url = upload_result["url"]
        object_name = upload_result["object_name"]

        try:
            success = await upload_to_oss(client, put_url, image_bytes, content_type)
        except Exception:
            raise HTTPException(status_code=502, detail="Failed to upload image to OSS.")

        if not success:
            raise HTTPException(status_code=502, detail="OSS upload returned non-200 status.")

        cdn_url = "https://cdn.imgupscaler.ai/" + object_name

        try:
            job_id = await create_job(client, cdn_url, request.prompt)
        except Exception:
            raise HTTPException(status_code=502, detail="Failed to create editing job.")

        try:
            for _ in range(60):
                await asyncio.sleep(3)
                result = await check_job(client, job_id)
                if result.get("code") != 300006:
                    break
            else:
                raise HTTPException(status_code=504, detail="Job timed out.")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=502, detail="Failed to retrieve job status.")

        output_urls = result.get("result", {}).get("output_url", [])
        if not output_urls:
            raise HTTPException(status_code=502, detail="No output image returned.")

        return EditResponse(
            success=True,
            job_id=job_id,
            image=output_urls[0],
        )