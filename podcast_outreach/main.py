# podcast_outreach/main.py

# IMPORTANT: Apply Windows optimizations FIRST, before any other imports
from podcast_outreach.windows_socket_config import apply_windows_optimizations
apply_windows_optimizations()

import os
import uuid
import logging
from datetime import datetime
from contextlib import asynccontextmanager
import sys
import threading
import asyncio 
import secrets # For session secret key

# Session configuration
ONE_HOUR_IN_SECONDS = 3600

# Project-specific imports from the new structure
from podcast_outreach.config import ENABLE_LLM_TEST_DASHBOARD, PORT, FRONTEND_ORIGIN, IS_PRODUCTION # Import FRONTEND_ORIGIN
from podcast_outreach.logging_config import setup_logging, get_logger
from podcast_outreach.api.dependencies import (
    authenticate_user_details, 
    prepare_session_data, 
    get_current_user,
    get_admin_user
)
from podcast_outreach.api.middleware import AuthMiddleware 
from podcast_outreach.database.connection import init_db_pool, close_db_pool  
from podcast_outreach.services.tasks.manager import task_manager # New path for task_manager
from podcast_outreach.services.scheduler.task_scheduler import initialize_scheduler
from podcast_outreach.services.events.event_bus import initialize_event_handlers

# Import the AI usage tracker from its new location
from podcast_outreach.services.ai.tracker import tracker as ai_tracker

# Import FastAPI and Jinja2Templates
from fastapi import FastAPI, Request, Query, Response, status, Form, Depends, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Import CORS middleware
from fastapi.middleware.cors import CORSMiddleware # <--- ADD THIS

# Session Middleware
from starlette.middleware.sessions import SessionMiddleware # Import SessionMiddleware

# Import the new API routers
from podcast_outreach.api.routers import campaigns, matches, media, pitches, tasks, auth, people,webhooks, ai_usage, review_tasks, placements, users, dashboard, client, media_kits, pitch_templates,storage as storage_router, episodes as episodes_router, scheduler as scheduler_router, billing, chatbot
# Add the new public_lead_magnet router import
from podcast_outreach.api.routers import public_lead_magnet
# Add notifications router for real-time updates
from podcast_outreach.api.routers import notifications
# Add OAuth router for social authentication
from podcast_outreach.api.routers import oauth

setup_logging()
logger = get_logger(__name__)

# Conditionally import and register routes from test_runner.py
ENABLE_LLM_TEST_DASHBOARD = os.getenv("ENABLE_LLM_TEST_DASHBOARD", "false").lower() == "true"

async def _delayed_scheduler_start(scheduler):
    """Delayed scheduler start to prevent memory spikes on startup."""
    await asyncio.sleep(60)  # 60 second delay
    logger.info("Starting task scheduler after startup delay...")
    scheduler.running = True
    logger.info("Task scheduler is now active and processing tasks.")

# Define lifespan context manager before app initialization
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup logic
    logger.info("Application starting up...")
    
    # Initialize DB pool
    await init_db_pool()
    logger.info("Database connection pool initialized.")
    
    # Initialize TaskManager database resources
    await task_manager.initialize()
    logger.info("TaskManager initialized.")
    
    # Initialize event-driven workflow orchestration
    initialize_event_handlers()
    logger.info("Event handlers initialized.")
    
    # Initialize notification service for real-time updates
    from podcast_outreach.services.events.notification_service import get_notification_service
    notification_service = get_notification_service()
    logger.info("Notification service initialized.")
    
    # Initialize and start task scheduler
    scheduler = initialize_scheduler(task_manager)
    
    # In production, delay scheduler start to prevent memory spikes
    if IS_PRODUCTION:
        logger.info("Production mode: Implementing 60-second startup delay to prevent memory spikes")
        # Start scheduler but don't let it run tasks immediately
        scheduler.running = False
        asyncio.create_task(_delayed_scheduler_start(scheduler))
        logger.info("Task scheduler initialized, will start after delay.")
    else:
        await scheduler.start()
        logger.info("Task scheduler started.")

    if ENABLE_LLM_TEST_DASHBOARD:
        logger.info("ENABLE_LLM_TEST_DASHBOARD is true. Attempting to load test runner routes.")
        # Get the absolute path to the project root (PGL/)
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        # Get the absolute path to the tests directory
        tests_dir = os.path.join(project_root, "tests")

        if not os.path.isdir(tests_dir):
            logger.warning(f"Tests directory not found at {tests_dir}. Cannot load LLM Test Dashboard.")
        else:
            original_sys_path = list(sys.path)
            # Add tests directory to sys.path to allow importing test_runner
            # and also the project root to allow test_runner to import src modules like auth_middleware
            if tests_dir not in sys.sys_path: # Corrected from sys.path
                sys.path.insert(0, tests_dir)
            if project_root not in sys.sys_path: # Corrected from sys.path
                sys.path.insert(0, project_root)
            
            try:
                from test_runner import register_routes as register_test_routes # type: ignore
                logger.info("Successfully imported register_routes from test_runner.")
                register_test_routes(app) # Call the registration here
                logger.info("LLM Test Dashboard routes registered.")
            except ImportError as e:
                logger.error(f"Failed to import test_runner. LLM Test Dashboard will not be available. Error: {e}")
            except Exception as e:
                logger.error(f"An unexpected error occurred while trying to import test_runner: {e}")
            finally:
                # Restore original sys.path
                sys.path = original_sys_path
    else:
        logger.info("ENABLE_LLM_TEST_DASHBOARD is false. LLM Test Dashboard routes will not be loaded.")
    
    yield  # This is where FastAPI serves requests
    
    # Shutdown logic
    try:
        logger.info("Application shutting down, cleaning up resources...")
        
        # Stop task scheduler
        from podcast_outreach.services.scheduler.task_scheduler import get_scheduler
        scheduler = get_scheduler()
        if scheduler:
            await scheduler.stop()
            logger.info("Task scheduler stopped.")
        
        # Clean up any running tasks or processes
        if hasattr(task_manager, 'cleanup'):
            await task_manager.cleanup()
        
        # Close any open database connections or services
        await close_db_pool()  # Close DB pool
        logger.info("Database connection pool closed.")
        
        # Allow some time for graceful cleanup
        await asyncio.sleep(0.5)
        
        logger.info("Cleanup completed")
    except Exception as e:
        logger.error(f"Error during application shutdown: {e}", exc_info=True)

# Initialize FastAPI app with lifespan context manager
app = FastAPI(lifespan=lifespan)

# CRITICAL: Add SessionMiddleware IMMEDIATELY after app creation
# This must be done before any other configuration
SESSION_SECRET_KEY = os.getenv("SESSION_SECRET_KEY")
if not SESSION_SECRET_KEY:
    logger.warning(
        "SESSION_SECRET_KEY is not set in environment variables. "
        "Using a default temporary key. THIS IS NOT SAFE FOR PRODUCTION. "
        "Please set a strong, unique SESSION_SECRET_KEY in your .env file."
    )
    SESSION_SECRET_KEY = secrets.token_hex(32)

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET_KEY,
    max_age=ONE_HOUR_IN_SECONDS,  # Session expires after 1 hour
    session_cookie="pgl_session_id",
    path="/",
    same_site="none" if IS_PRODUCTION else "lax",  # 'none' for production cross-origin, 'lax' for local dev
    https_only=IS_PRODUCTION,  # Only require HTTPS in production
    domain=None  # Let the browser handle the domain
)

# --- Session Middleware Configuration ---

# Import the CORS configuration helper
from podcast_outreach.config_cors import get_allowed_origins

# Get allowed origins from the configuration
origins = get_allowed_origins()
logger.info(f"CORS origins configured: {origins}")

# Add middleware in correct order - middleware executes in REVERSE order of addition
# SessionMiddleware was added first (right after app creation)
# Now adding: CORS, then Auth
# Final execution order: Session -> Auth -> CORS

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins, # Now origins is defined
    allow_credentials=True, # Allow cookies
    allow_methods=["*"],    # Allow all methods
    allow_headers=["*"],    # Allow all headers
)

# Add resource cleanup middleware (before auth middleware)
from podcast_outreach.api.middleware_cleanup import ResourceCleanupMiddleware, ConnectionMonitorMiddleware
app.add_middleware(ResourceCleanupMiddleware, cleanup_frequency=50)
app.add_middleware(ConnectionMonitorMiddleware)

# Add authentication middleware
app.add_middleware(AuthMiddleware)

# Initialize Jinja2Templates for HTML rendering
templates = Jinja2Templates(directory="podcast_outreach/templates") # Explicitly set path

# Register custom filters for templates
def format_number(value):
    """Format a number with comma separators"""
    return f"{value:,}"

def format_datetime(timestamp):
    """Convert ISO timestamp or datetime object to readable format"""
    if isinstance(timestamp, str):
        try:
            dt = datetime.fromisoformat(timestamp)
        except ValueError:
            return timestamp # Return original if cannot parse
    elif isinstance(timestamp, datetime):
        dt = timestamp
    else:
        return str(timestamp) # Fallback for unexpected types

    return dt.strftime("%Y-%m-%d %H:%M:%S")

templates.env.filters["format_number"] = format_number
templates.env.filters["format_datetime"] = format_datetime

# Mount static files directory
app.mount("/static", StaticFiles(directory="podcast_outreach/static"), name="static") # Explicitly set path

# Include API routers
app.include_router(storage_router.router)
app.include_router(campaigns.router)
app.include_router(matches.router)
app.include_router(media.router)
app.include_router(pitches.router)
app.include_router(tasks.router)
app.include_router(auth.router)
app.include_router(people.router)
app.include_router(webhooks.router) 
app.include_router(ai_usage.router)
app.include_router(review_tasks.router)
app.include_router(placements.router)
app.include_router(users.router)
app.include_router(dashboard.router)
app.include_router(client.router)
app.include_router(media_kits.router)
app.include_router(pitch_templates.router)
app.include_router(episodes_router.router)
app.include_router(scheduler_router.router)
app.include_router(public_lead_magnet.router)
app.include_router(notifications.router)
app.include_router(billing.router)
app.include_router(oauth.router)
app.include_router(chatbot.router)


@app.get("/login")
def login_page(request: Request):
    """Render the login page"""
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login")
async def login(
    request: Request,
    username: str = Form(...), # This form field name is 'username' from HTML, but we treat it as email
    password: str = Form(...)
):
    email_to_auth = username # Treat the form's 'username' field as the email
    user_details = await authenticate_user_details(email_to_auth, password)
    
    if not user_details:
        return templates.TemplateResponse(
            "login.html", 
            {
                "request": request, 
                "error": "Invalid email or password" # Updated message
            }
        )
    
    # Store user details in the session
    session_data = prepare_session_data(user_details)
    request.session.update(session_data) # StarletteSessionMiddleware provides request.session
    
    logger.info(f"User '{user_details['username']}' logged in via form. Session data set.")
    
    # RedirectResponse will carry the session cookie set by the middleware
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/logout")
async def logout(request: Request): # request is needed to clear session
    request.session.clear() # Clear the session data
    logger.info("User logged out. Session cleared.")
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    # The session cookie itself will be managed by the middleware (e.g., cleared or expired)
    # If you need to explicitly delete the cookie:
    # response.delete_cookie(key="session", httponly=True, secure=True, samesite="lax") # Default cookie name is "session"
    return response


@app.get("/")
def index(request: Request, user: dict = Depends(get_current_user)):
    """
    Root endpoint that renders the main dashboard HTML.
    Requires authentication.
    """
    return templates.TemplateResponse(
        "dashboard.html", 
        {
            "request": request,
            "username": user["username"],
            "role": user["role"]
        }
    )


@app.get("/api-status")
def api_status():
    """
    API status endpoint that returns a simple JSON message.
    This endpoint is public.
    """
    return {"message": "PGL Automation API is running (FastAPI version)!"}


@app.get("/admin")
def admin_dashboard(request: Request, user: dict = Depends(get_admin_user)):
    """
    Admin dashboard page that shows links to all admin-only features.
    Admin access required.
    """
    return templates.TemplateResponse(
        "admin_dashboard.html", 
        {
            "request": request,
            "username": user["username"],
            "role": user["role"]
        }
    )

if __name__ == "__main__":
    import uvicorn

    port = PORT if isinstance(PORT, int) else 8000 # Ensure PORT is int, fallback
    logger.info(f"Starting FastAPI app on port {port}.")
    uvicorn.run(app, host='0.0.0.0', port=port)