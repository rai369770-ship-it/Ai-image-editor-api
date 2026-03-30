import base64
import asyncio
import json

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI(
    title="Image Editor API",
    version="1.0.0",
    description="AI image editing service powered by AIEnhancer.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

AES_KEY = b"ai-enhancer-web__aes-key"
AES_IV = b"aienhancer-aesiv"

HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://aienhancer.ai",
    "Referer": "https://aienhancer.ai/ai-image-editor",
}


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


def encrypt(obj: dict) -> str:
    plaintext = json.dumps(obj).encode("utf-8")
    padded = pad(plaintext, AES.block_size)
    cipher = AES.new(AES_KEY, AES.MODE_CBC, AES_IV)
    encrypted = cipher.encrypt(padded)
    return base64.b64encode(encrypted).decode("utf-8")


async def nsfw_check(client: httpx.AsyncClient, image: str) -> str:
    response = await client.post(
        "https://aienhancer.ai/api/v1/r/nsfw-detection",
        json={"image": image},
        headers=HEADERS,
    )
    response.raise_for_status()
    task_id = response.json()["data"]["id"]

    while True:
        await asyncio.sleep(2)
        res = await client.post(
            "https://aienhancer.ai/api/v1/r/nsfw-detection/result",
            json={"task_id": task_id},
            headers=HEADERS,
        )
        res.raise_for_status()
        data = res.json()["data"]
        if data["status"] == "succeeded":
            return data["output"]


async def image_editor(client: httpx.AsyncClient, image: str, prompt: str) -> dict:
    settings = encrypt({
        "prompt": prompt,
        "size": "2K",
        "aspect_ratio": "match_input_image",
        "output_format": "jpeg",
        "max_images": 1,
    })

    response = await client.post(
        "https://aienhancer.ai/api/v1/k/image-enhance/create",
        json={
            "model": 2,
            "image": image,
            "function": "ai-image-editor",
            "settings": settings,
        },
        headers=HEADERS,
    )
    response.raise_for_status()
    task_id = response.json()["data"]["id"]

    while True:
        await asyncio.sleep(2.5)
        res = await client.post(
            "https://aienhancer.ai/api/v1/k/image-enhance/result",
            json={"task_id": task_id},
            headers=HEADERS,
        )
        res.raise_for_status()
        data = res.json()["data"]
        if data["status"] == "success":
            return {
                "id": task_id,
                "output": data["output"],
                "input": data["input"],
            }


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

    b64 = base64.b64encode(image_bytes).decode("utf-8")
    image_data_url = f"data:image/jpeg;base64,{b64}"

    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
        try:
            nsfw_result = await nsfw_check(client, image_data_url)
        except Exception:
            raise HTTPException(status_code=502, detail="Failed NSFW check.")

        if nsfw_result != "normal":
            raise HTTPException(status_code=400, detail="NSFW image blocked.")

        try:
            result = await image_editor(client, image_data_url, request.prompt)
        except Exception:
            raise HTTPException(status_code=502, detail="Failed to process image.")

        return EditResponse(
            success=True,
            job_id=result["id"],
            image=result["output"],
        )