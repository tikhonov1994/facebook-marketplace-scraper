from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class RawAdvertisement(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: str = Field(min_length=1, max_length=64)
    external_id: str | None = Field(default=None, max_length=255)
    url: HttpUrl | None = None
    title: str | None = None
    text: str = Field(min_length=1)
    author_name: str | None = None
    author_id: str | None = None
    published_at: datetime | None = None
    collected_at: datetime = Field(default_factory=lambda: datetime.now().astimezone())
    raw_data: dict[str, object] | None = None
