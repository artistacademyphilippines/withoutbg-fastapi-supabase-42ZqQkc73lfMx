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

# ============================================
# LOAD ENVIRONMENT VARIABLES FROM DIGITAL OCEAN
# ============================================
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET")

# Check if all required environment variables are set
if not all([SUPABASE_URL, SUPABASE_SERVICE_KEY, SUPABASE_JWT_SECRET]):
    raise RuntimeError("Missing SUPABASE environment variables")

# ============================================
# INITIALIZE FASTAPI APP
# ============================================
app = FastAPI()

# Allow requests from these origins (your frontend)
origins = ["*"]  # Allow all origins - restrict in production

# Add CORS middleware to allow frontend to call this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================
# INITIALIZE WITHOUTBG MODEL
# ============================================
bg_remover = None  # Will store the loaded model

@app.on_event("startup")
async def startup_event():
    """This runs once when the server starts"""
    global bg_remover
    print("[√] Loading WithoutBG Focus v1.5 model...")
    try:
        # Load the best quality model (Focus v1.5)
        bg_remover = WithoutBG.opensource(model="Focus-v1.5")
        print("[√] Model loaded successfully")
    except Exception as e:
        print(f"[X] Failed to load model: {e}")
        raise

# ============================================
# REQUEST DATA MODEL
# ============================================
class RequestData(BaseModel):
    """Defines what data we expect from the frontend"""
    data_sent: str  # Base64 encoded image string

# ============================================
# HELPER: GET USER CREDITS FROM SUPABASE
# ============================================
async def get_user_credits(user_email: str) -> int:
    """
    Fetches the user's credit balance from Supabase
    Returns: number of credits (int)
    Raises: HTTPException if connection fails or user not found
    """
    from urllib.parse import quote
    safe_email = quote(user_email)  # Encode email for URL safety
    
    # Make HTTP GET request to Supabase
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{SUPABASE_URL}/rest/v1/wondr_users?select=rembg_credits&email=eq.{safe_email}",
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Accept": "application/json",
                "Accept-Profile": "wondr_users",
            },
        )
    
    # Check if request to Supabase failed
    if response.status_code != 200:
        print(f"[X] Supabase GET failed: {response.status_code}")
        raise HTTPException(status_code=300, detail="Failed to connect to Supabase")
    
    # Parse the JSON response
    data = response.json()
    
    # Check if user exists in database
    if not data:
        print(f"[X] User not found: {user_email}")
        raise HTTPException(status_code=300, detail="User not found in database")
    
    # Return the credit balance
    credits = data[0]["rembg_credits"]
    print(f"[√] User {user_email} has {credits} credits")
    return credits

# ============================================
# HELPER: DEDUCT ONE CREDIT FROM USER
# ============================================
async def deduct_credit(user_email: str) -> int:
    """
    Deducts 1 credit from user's balance
    Returns: new credit balance (int)
    Raises: HTTPException if insufficient credits or connection fails
    """
    # First, get current credits
    current_credits = await get_user_credits(user_email)
    
    # Check if user has enough credits
    if current_credits <= 0:
        print(f"[X] Insufficient credits for {user_email}")
        raise HTTPException(status_code=400, detail="No credits remaining")
    
    # Calculate new balance
    new_credits = current_credits - 1
    
    from urllib.parse import quote
    safe_email = quote(user_email)
    
    # Make HTTP PATCH request to update credits in Supabase
    async with httpx.AsyncClient() as client:
        response = await client.patch(
            f"{SUPABASE_URL}/rest/v1/wondr_users?email=eq.{safe_email}",
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=representation",
                "Accept-Profile": "wondr_users",
                "Content-Profile": "wondr_users",
            },
            json={"rembg_credits": new_credits}  # Update the credit value
        )
    
    # Check if update failed
    if response.status_code not in [200, 204]:
        print(f"[X] Supabase PATCH failed: {response.status_code}")
        raise HTTPException(status_code=300, detail="Failed to update credits")
    
    print(f"[√] Credit deducted. New balance: {new_credits}")
    return new_credits

# ============================================
# HELPER: REFUND CREDIT IF PROCESSING FAILS
# ============================================
async def refund_credit(user_email: str):
    """
    Adds 1 credit back to user's balance if image processing fails
    This ensures users don't lose credits when errors occur
    """
    try:
        # Get current credits
        current_credits = await get_user_credits(user_email)
        
        from urllib.parse import quote
        safe_email = quote(user_email)
        
        # Add 1 credit back
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
        print(f"[√] Credit refunded to {user_email}")
    except Exception as e:
        print(f"[X] Refund failed: {e}")

# ============================================
# ROUTE: CHECK IF API IS RUNNING
# ============================================
@app.get("/")
async def root():
    """Simple health check endpoint"""
    return {"status": "running"}

# ============================================
# ROUTE: REMOVE BACKGROUND FROM IMAGE
# ============================================
@app.post("/")
async def remove_background(
    request_data: RequestData,  # The image data from frontend
    authorization: str = Header(None)  # The JWT token from frontend
):
    """
    Main endpoint that:
    1. Validates the user's token
    2. Checks and deducts credits
    3. Processes the image
    4. Returns the result
    """
    
    # ----------------------------------------
    # STEP 1: VALIDATE AUTHORIZATION TOKEN
    # ----------------------------------------
    print("\n[√] New request received")
    
    # Check if Authorization header exists
    if not authorization:
        print("[X] Missing Authorization header")
        raise HTTPException(status_code=100, detail="Missing authorization")
    
    # Check if it starts with "Bearer "
    if not authorization.startswith("Bearer "):
        print("[X] Invalid Authorization format")
        raise HTTPException(status_code=100, detail="Invalid authorization format")
    
    # Extract the actual token (remove "Bearer " prefix)
    token = authorization.split(" ")[1]
    
    # ----------------------------------------
    # STEP 2: DECODE TOKEN TO GET USER EMAIL
    # ----------------------------------------
    try:
        # Decode the JWT token using the secret key
        payload = jwt.decode(
            token,
            SUPABASE_JWT_SECRET,
            algorithms=["HS256"],  # Algorithm used by Supabase
            options={"verify_aud": False}  # Don't verify audience
        )
        
        # Extract user email from token payload
        user_email = payload.get("email")
        
        # Check if email exists in token
        if not user_email:
            print("[X] Email missing in token")
            raise HTTPException(status_code=100, detail="Invalid token payload")
        
        print(f"[√] User authenticated: {user_email}")
        
    except jwt.ExpiredSignatureError:
        # Token has expired
        print("[X] Token expired")
        raise HTTPException(status_code=100, detail="Token expired")
    except jwt.InvalidTokenError as e:
        # Token is invalid or tampered
        print(f"[X] Invalid token: {e}")
        raise HTTPException(status_code=100, detail="Invalid token")
    
    # ----------------------------------------
    # STEP 3: CHECK AND DEDUCT CREDITS
    # ----------------------------------------
    try:
        # This will check credits and deduct 1 if available
        remaining_credits = await deduct_credit(user_email)
        print(f"[√] Credits deducted. Remaining: {remaining_credits}")
    except HTTPException:
        # If it's already an HTTPException, just re-raise it
        # This preserves our custom error codes (300 or 400)
        raise
    except Exception as e:
        # Any other unexpected error
        print(f"[X] Unexpected error during credit check: {e}")
        raise HTTPException(status_code=300, detail="Credit system error")
    
    # ----------------------------------------
    # STEP 4: DECODE THE IMAGE
    # ----------------------------------------
    try:
        # Check if data has the "data:image/..." prefix
        if "," in request_data.data_sent:
            # Remove the prefix and decode base64
            img_data = base64.b64decode(request_data.data_sent.split(",")[1])
        else:
            # Already pure base64, just decode
            img_data = base64.b64decode(request_data.data_sent)
        
        print(f"[√] Image decoded: {len(img_data)} bytes")
        
        # Convert bytes to PIL Image object
        input_image = Image.open(BytesIO(img_data))
        print(f"[√] Image loaded: {input_image.size}, mode: {input_image.mode}")
        
        # Convert to RGB if needed (WithoutBG requires RGB)
        if input_image.mode != "RGB":
            input_image = input_image.convert("RGB")
            print(f"[√] Converted to RGB")
        
    except Exception as e:
        # Image decode failed - refund credit and return error
        print(f"[X] Failed to decode image: {e}")
        await refund_credit(user_email)
        raise HTTPException(status_code=500, detail="Invalid image data")
    
    # ----------------------------------------
    # STEP 5: REMOVE BACKGROUND
    # ----------------------------------------
    try:
        # Check if model is loaded
        if bg_remover is None:
            print("[X] Model not loaded")
            await refund_credit(user_email)
            raise HTTPException(status_code=500, detail="Model not loaded")
        
        # Process the image with WithoutBG
        print("[√] Processing image...")
        output_image = bg_remover.remove_background(input_image)
        print(f"[√] Background removed: {output_image.size}")
        
        # ----------------------------------------
        # STEP 6: CONVERT TO WEBP
        # ----------------------------------------
        buffer = BytesIO()
        output_image.save(
            buffer,
            format="WEBP",  # Modern format with transparency support
            quality=100,  # Maximum quality
            method=6,  # Best compression (slower but better quality)
            lossless=True  # No quality loss
        )
        buffer.seek(0)  # Reset buffer position to start
        
        print(f"[√] WebP created: {len(buffer.getvalue())} bytes")
        
        # ----------------------------------------
        # STEP 7: ENCODE TO BASE64
        # ----------------------------------------
        # Convert bytes to base64 string
        encoded = base64.b64encode(buffer.getvalue()).decode("utf-8")
        
        # Add data URL prefix for browser compatibility
        result_data = f"data:image/webp;base64,{encoded}"
        
        print("[√] Success! Returning result")
        
        # ----------------------------------------
        # RETURN SUCCESS (CODE 200)
        # ----------------------------------------
        return {
            "data_received": result_data,
            "remaining_credits": remaining_credits
        }
        
    except Exception as e:
        # Processing failed - refund credit and return error
        print(f"[X] Processing failed: {e}")
        await refund_credit(user_email)
        raise HTTPException(status_code=500, detail="Processing failed")
