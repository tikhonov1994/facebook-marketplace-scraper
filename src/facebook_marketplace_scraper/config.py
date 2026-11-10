from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    facebook_storage_state: str = "./data/facebook/storage_state.json"
    facebook_profile_dir: str = "./data/facebook/browser_profile"
    facebook_photo_browsing_enabled: bool = True
    facebook_photo_browsing_visit_limit: int = Field(default=3, ge=0, le=10)
    facebook_photo_browsing_max_steps: int = Field(default=2, ge=0, le=5)
    facebook_seller_browsing_enabled: bool = True
    facebook_seller_browsing_max_cards: int = Field(default=3, ge=1, le=5)
    facebook_seller_listings_enabled: bool = True
    facebook_seller_page_limit: int = Field(default=5, ge=0, le=50)
    facebook_price_split_enabled: bool = True
    facebook_price_split_threshold: int = Field(default=100, ge=2, le=500)
    facebook_price_split_max_searches: int = Field(default=4, ge=0, le=20)
    facebook_detail_limit: int = Field(default=200, ge=1, le=2000)
    facebook_max_scrolls: int = Field(default=20, ge=1, le=100)
    facebook_location_id: str = "112306758786227"
    facebook_location_name: str = "Pattaya"
    facebook_radius_km: int = Field(default=10, ge=1, le=500)
    facebook_history_limit: int = Field(default=100, ge=1, le=500)
    facebook_page_timeout_seconds: int = Field(default=30, ge=5, le=120)
    facebook_browser_executable: str | None = None
    facebook_check_pause_seconds: int = Field(default=5, ge=1, le=60)


def get_settings() -> Settings:
    return Settings()
