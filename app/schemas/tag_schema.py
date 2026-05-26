from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime

class AnchorCreate(BaseModel):
    tag_name: str = Field(..., max_length=100)
    namespace: str = Field(default="general", max_length=50)
    image_paths: List[str]

class TagResponse(BaseModel):
    name: str
    category: Optional[str] = None
    frequency: int = 0

class TagVoteRequest(BaseModel):
    is_upvote: bool

class ArtworkTagValidationRequest(BaseModel):
    is_approved: bool
