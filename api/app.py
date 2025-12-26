from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
import base64
from io import BytesIO
from PIL import Image
import httpx
import jwt
import torch
from withoutbg import WithoutBG

# -------------------
# Environment variables
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET")

if not all([SUPABASE_URL, SUPABASE_SERVICE_KEY, SUPABASE_JWT_SECRET]):
    raise RuntimeError("Missing SUPABASE environment variables")

# -------------------
app = FastAPI()

# Update origins to allow your actual frontend
origins = [
    "http://127.0.0.1:5503",
    "http://localhost:5503",
    "*"  # Allow all origins for testing, restrict in production
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------
# Initialize WithoutBG model (use CPU for Digital Ocean compatibility)
device = "cuda" if torch.cuda.is_available() else "cpu"
print(f"Using device: {device}")

# Initialize the model on startup
bg_remover = None

@app.on_event("startup")
async def startup_event():
    global bg_remover
    print("Loading WithoutBG model...")
    try:
        bg_remover = WithoutBG(
            model_name="u2net",  # Options: "u2net", "u2net_human_seg", "u2netp"
            device=device
        )
        print("WithoutBG model loaded successfully")
    except Exception as e:
        print(f"Error loading WithoutBG model: {e}")
        raise

# -------------------
class RequestData(BaseModel):
    data_sent: str

# -------------------
async def get_user_credits(user_email: str) -> int:
    """Get user's current rembg_credits by email"""
    from urllib.parse import quote
    safe_email = quote(user_email)
    
    async with httpx.AsyncClient() as client:
        res = await client.get(
            f"{SUPABASE_URL}/rest/v1/wondr_users?select=rembg_credits&email=eq.{safe_email}",
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Accept": "application/json",
                "Accept-Profile": "wondr_users",
            },
        )
    
    print("GET credits status:", res.status_code)
    print("GET credits response:", res.text)
    
    if res.status_code != 200:
        raise HTTPException(status_code=500, detail="Failed to fetch credits")
    
    data = res.json()
    if not data:
        raise HTTPException(status_code=404, detail="User not found in wondr_users table")
    
    return data[0]["rembg_credits"]

async def deduct_credit(user_email: str) -> int:
    """Deduct 1 credit from user and return new balance"""
    # Get current credits first
    current_credits = await get_user_credits(user_email)
    
    if current_credits <= 0:
        raise HTTPException(status_code=403, detail="Insufficient rembg credits")
    
    # Deduct 1 credit
    new_credits = current_credits - 1
    
    from urllib.parse import quote
    safe_email = quote(user_email)
    
    async with httpx.AsyncClient() as client:
        res = await client.patch(
            f"{SUPABASE_URL}/rest/v1/wondr_users?email=eq.{safe_email}",
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=representation",
                "Accept-Profile": "wondr_users",
                "Content-Profile": "wondr_users",
            },
            json={"rembg_credits": new_credits}
        )
    
    print("PATCH credits status:", res.status_code)
    print("PATCH credits response:", res.text)
    
    if res.status_code not in [200, 204]:
        raise HTTPException(status_code=500, detail="Failed to deduct credit")
    
    return new_credits

async def refund_credit(user_email: str):
    """Refund 1 credit to user"""
    try:
        current_credits = await get_user_credits(user_email)
        
        from urllib.parse import quote
        safe_email = quote(user_email)
        
        async with httpx.AsyncClient() as client:
            await client.patch(
                f"{SUPABASE_URL}/rest/v1/wondr_users?email=eq.{safe_email}",
                headers={
                    "apikey": SUPABASE_SERVICE_KEY,
                    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                    "Content-Type": "application/json",
                    "Accept-Profile": "wondr_users",
                    "Content-Profile": "wondr_users",
                },
                json={"rembg_credits": current_credits + 1}
            )
        print(f"Credit refunded to user {user_email}")
    except Exception as e:
        print(f"Failed to refund credit: {e}")

# -------------------
@app.get("/")
async def root():
    return {"status": "FastAPI WithoutBG service is running", "device": device}

@app.post("/")
async def remove_background(request_data: RequestData, authorization: str = Header(None)):
    # Validate authorization header
    print("=== DEBUG: Authorization Header ===")
    print(f"Authorization header present: {authorization is not None}")
    
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    
    if not authorization.startswith("Bearer "):
        print(f"Authorization format: {authorization[:20]}...")
        raise HTTPException(status_code=401, detail="Authorization must be 'Bearer <token>'")
    
    token = authorization.split(" ")[1]
    print(f"Token (first 50 chars): {token[:50]}...")
    
    # Decode JWT token to get user info
    try:
        print("Attempting to decode token...")
        payload = jwt.decode(
            token, 
            SUPABASE_JWT_SECRET, 
            algorithms=["HS256"],
            options={"verify_aud": False}
        )
        print(f"Token payload: {payload}")
        
        user_id = payload.get("sub")
        user_email = payload.get("email")
        
        if not user_id:
            raise HTTPException(status_code=401, detail="Token missing user ID (sub claim)")
        
        print(f"✓ Authenticated user: {user_email} (ID: {user_id})")
        
    except jwt.ExpiredSignatureError:
        print("Token expired")
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError as e:
        print(f"JWT decode error: {e}")
        print(f"JWT Secret (first 10 chars): {SUPABASE_JWT_SECRET[:10]}...")
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")
    
    # Check and deduct credits
    try:
        remaining_credits = await deduct_credit(user_email)
        print(f"Credits deducted. Remaining: {remaining_credits}")
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error deducting credits: {e}")
        raise HTTPException(status_code=500, detail="Failed to process credits")
    
    # Decode image
    try:
        if "," in request_data.data_sent:
            img_data = base64.b64decode(request_data.data_sent.split(",")[1])
        else:
            img_data = base64.b64decode(request_data.data_sent)
        
        # Convert to PIL Image
        input_image = Image.open(BytesIO(img_data))
        # Convert to RGB if necessary (WithoutBG expects RGB)
        if input_image.mode != "RGB":
            input_image = input_image.convert("RGB")
            
    except Exception as e:
        print(f"Failed to decode image: {e}")
        await refund_credit(user_email)
        raise HTTPException(status_code=400, detail="Invalid image data")
    
    # Remove background with WithoutBG
    try:
        print("Removing background with WithoutBG...")
        # WithoutBG returns a PIL Image with transparency
        output_image = bg_remover(input_image)
        
        # Convert to PNG bytes with transparency
        output_buffer = BytesIO()
        output_image.save(output_buffer, format="PNG", optimize=True)
        output_buffer.seek(0)
        
        # Encode to base64
        new_base64 = base64.b64encode(output_buffer.getvalue()).decode("utf-8")
        data_received = f"data:image/png;base64,{new_base64}"
        
        print("Background removed successfully")
        
        return {
            "data_received": data_received,
            "remaining_credits": remaining_credits
        }
    except Exception as e:
        print(f"Failed to remove background: {e}")
        await refund_credit(user_email)
        raise HTTPException(status_code=500, detail=f"Failed to process image: {str(e)}")