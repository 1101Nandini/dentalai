from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List, Optional
import os
from google.oauth2 import service_account


class Settings(BaseSettings):
    # -----------------------------------------------------------------------
    # App
    # -----------------------------------------------------------------------
    APP_NAME: str = "Smile Guide AI – Dental AI"
    DEBUG: bool = False
    SECRET_KEY: str = "change-me-in-production"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 720  # 12 hours
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    FRONTEND_URL: str = "https://app.smileguideai.com"  
    # FRONTEND_URL: str = "http://localhost:3000"  

    # -----------------------------------------------------------------------
    # CORS
    # -----------------------------------------------------------------------
    ALLOWED_ORIGINS: List[str] = ["http://localhost:3000", "http://localhost:5173", "http://127.0.0.1:3000", "http://192.168.0.131:3000", "http://192.168.0.172:3000", "https://app.smileguideai.com"]
    

    # -----------------------------------------------------------------------
    # MongoDB Atlas
    # -----------------------------------------------------------------------
    MONGODB_URI: str = "mongodb+srv://<user>:<password>@cluster.mongodb.net"
    MONGODB_DB_NAME: str = "dental_ai"

    # -----------------------------------------------------------------------
    # Azure
    # -----------------------------------------------------------------------
    AZURE_STORAGE_CONNECTION_STRING: str = ""
    AZURE_BLOB_CONTAINER_NAME: str = "dental-documents"
    AZURE_STORAGE_ACCOUNT_NAME: str = ""
    AZURE_STORAGE_ACCOUNT_KEY: str = ""
    AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT: str = ""
    AZURE_DOCUMENT_INTELLIGENCE_KEY: str = ""
    AZURE_VISION_ENDPOINT: str = ""
    AZURE_VISION_KEY: str = ""
    
    # -----------------------------------------------------------------------
    # Azure Speech
    # -----------------------------------------------------------------------
    AZURE_SPEECH_ENDPOINT: str = ""
    AZURE_SPEECH_KEY: str = ""
    AZURE_SPEECH_TOKEN_ENDPOINT: str = ""
    # -----------------------------------------------------------------------
    # Azure AI Search (Vector Store)
    # -----------------------------------------------------------------------
    AZURE_SEARCH_ENDPOINT: str = ""
    AZURE_SEARCH_KEY: str = ""
    AZURE_SEARCH_INDEX_TEXT: str = "dental-text-index"
    AZURE_SEARCH_INDEX_IMAGE: str = "dental-image-index"
    AZURE_SEARCH_INDEX_CDT: str = "dental-cdt-index"
    AZURE_SEARCH_INDEX_CASE_STUDY: str = "dental-casestudy-index"
    CASE_STUDY_SIMILARITY_THRESHOLD: float = 0.70

    # -----------------------------------------------------------------------
    # OpenAI
    # -----------------------------------------------------------------------
    AZURE_OPENAI_ENDPOINT: str = ""
    AZURE_OPENAI_API_KEY: str = ""
    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-large"
    AZURE_OPENAI_CHAT_DEPLOYMENT: str = "gpt-4o"
    AZURE_OPENAI_API_VERSION: str = "2024-12-01-preview"
    OPENAI_EMBEDDING_DIMENSIONS: int = 3072
    AZURE_OPENAI_EMBEDDING_ENDPOINT: str = ""
    AZURE_OPENAI_EMBEDDING_KEY: str = ""
    AZURE_OPENAI_EMBEDDING_DEPLOYMENT: str = "text-embedding-3-large"
    AZURE_OPENAI_WHISPER_DEPLOYMENT: str = "whisper"

    
    # -----------------------------------------------------------------------
    # Gemini / Vertex AI
    # -----------------------------------------------------------------------
    GEMINI_CHAT_MODEL: str = "gemini-3-flash-preview"
    GEMINI_ANALYSIS_MODEL: str = "gemini-3.1-flash-lite"  # Fast model for query analysis / pre-retrieval
    GEMINI_STT_MODEL: str = "gemini-3.6-flash"  # Model used for audio transcription (STT) — only audio-capable model available in us-central1
    GOOGLE_CLOUD_PROJECT: str = "sonic-harbor-492115-k0"
    GOOGLE_CLOUD_LOCATION: str = "global"
    GOOGLE_APPLICATION_CREDENTIALS: Optional[str] = "gcp-credentials.json"
    GOOGLE_API_KEY: str = ""  # Gemini Developer API key (for Files API access)

    def get_vertex_credentials(self):
        """Returns Google Service Account credentials if the JSON file exists."""
        if not self.GOOGLE_APPLICATION_CREDENTIALS:
            return None
            
        creds_path = self.GOOGLE_APPLICATION_CREDENTIALS
        if not os.path.isabs(creds_path):
            creds_path = os.path.abspath(os.path.join(os.getcwd(), creds_path))
            
        if os.path.exists(creds_path):
            try:
                return service_account.Credentials.from_service_account_file(
                    creds_path,
                    scopes=["https://www.googleapis.com/auth/cloud-platform"]
                )
            except Exception as e:
                print(f"Error loading Vertex credentials: {e}")
                return None
        return None

    # -----------------------------------------------------------------------
    # Stripe
    # -----------------------------------------------------------------------
    STRIPE_SECRET_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""

    # -----------------------------------------------------------------------
    # Google OAuth
    # -----------------------------------------------------------------------
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""

    # -----------------------------------------------------------------------
    # -----------------------------------------------------------------------
    # recaptcha
    # -----------------------------------------------------------------------
    RECAPTCHA_SECRET_KEY: str = ""

    # -----------------------------------------------------------------------
    # Email (SMTP)
    # -----------------------------------------------------------------------
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    EMAIL_FROM: str = "no-reply@petalai.com"

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        case_sensitive=True
    )


settings = Settings()
