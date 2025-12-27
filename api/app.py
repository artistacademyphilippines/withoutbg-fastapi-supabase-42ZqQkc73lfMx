from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
import base64
from io import BytesIO
from PIL import Image
import httpx
import jwt
from withoutbg import WithoutBG

# Environment variables
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET")

if not all([SUPABASE_URL, SUPABASE_SERVICE_KEY, SUPABASE_JWT_SECRET]):
    raise RuntimeError("Missing SUPABASE environment variables")

# FastAPI app
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Model
bg_remover = None

@app.on_event("startup")
async def startup_event():
    global bg_remover
    print("[√] Loading model...")
    bg_remover = WithoutBG.opensource()
    print("[√] Model loaded")

# Request model
class RequestData(BaseModel):
    data_sent: str

# Get credits
async def get_user_credits(user_id: str) -> int:
    from urllib.parse import quote
    safe_id = quote(user_id)
    
    async with httpx.AsyncClient() as client:
        res = await client.get(
            f"{SUPABASE_URL}/rest/v1/wondr_users?select=rembg_credits&uid=eq.{safe_id}",
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Accept-Profile": "wondr_users",
            },
        )
    
    if res.status_code != 200:
        raise HTTPException(status_code=300, detail="Supabase connection failed")
    
    data = res.json()
    if not data:
        raise HTTPException(status_code=300, detail="User not found")
    
    return data[0]["rembg_credits"]

# Deduct credit
async def deduct_credit(user_id: str) -> int:
    current = await get_user_credits(user_id)
    
    if current <= 0:
        raise HTTPException(status_code=400, detail="No credits")
    
    new_credits = current - 1
    
    from urllib.parse import quote
    safe_id = quote(user_id)
    
    async with httpx.AsyncClient() as client:
        res = await client.patch(
            f"{SUPABASE_URL}/rest/v1/wondr_users?uid=eq.{safe_id}",
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Content-Type": "application/json",
                "Accept-Profile": "wondr_users",
                "Content-Profile": "wondr_users",
            },
            json={"rembg_credits": new_credits}
        )
    
    if res.status_code not in [200, 204]:
        raise HTTPException(status_code=300, detail="Failed to update credits")
    
    return new_credits

# Refund credit
async def refund_credit(user_id: str):
    try:
        current = await get_user_credits(user_id)
        
        from urllib.parse import quote
        safe_id = quote(user_id)
        
        async with httpx.AsyncClient() as client:
            await client.patch(
                f"{SUPABASE_URL}/rest/v1/wondr_users?uid=eq.{safe_id}",
                headers={
                    "apikey": SUPABASE_SERVICE_KEY,
                    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                    "Content-Type": "application/json",
                    "Accept-Profile": "wondr_users",
                    "Content-Profile": "wondr_users",
                },
                json={"rembg_credits": current + 1}
            )
    except:
        pass

# Routes
@app.get("/")
async def root():
    return {"status": "running"}

@app.post("/")
async def remove_background(request_data: RequestData, authorization: str = Header(None)):
    # Auth
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=100, detail="Invalid auth")
    
    token = authorization.split(" ")[1]
    
    try:
        payload = jwt.decode(token, SUPABASE_JWT_SECRET, algorithms=["HS256"], options={"verify_aud": False})
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=100, detail="Invalid token")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=100, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=100, detail="Invalid token")
    
    # Credits
    try:
        remaining = await deduct_credit(user_id)
    except HTTPException:
        raise
    except:
        raise HTTPException(status_code=300, detail="Credit error")
    
    # Decode image
    try:
        img_data = base64.b64decode(request_data.data_sent.split(",")[1] if "," in request_data.data_sent else request_data.data_sent)
        input_img = Image.open(BytesIO(img_data)).convert("RGB")
    except:
        await refund_credit(user_id)
        raise HTTPException(status_code=500, detail="Invalid image")
    
    # Process
    try:
        if not bg_remover:
            await refund_credit(user_id)
            raise HTTPException(status_code=500, detail="Model not loaded")
        
        output_img = bg_remover.remove_background(input_img)
        
        buffer = BytesIO()
        output_img.save(buffer, format="WEBP", quality=100, method=6, lossless=True)
        buffer.seek(0)
        
        encoded = base64.b64encode(buffer.getvalue()).decode("utf-8")
        
        return {
            "data_received": f"data:image/webp;base64,{encoded}",
            "remaining_credits": remaining
        }
    except:
        await refund_credit(user_id)
        raise HTTPException(status_code=500, detail="Processing failed")

