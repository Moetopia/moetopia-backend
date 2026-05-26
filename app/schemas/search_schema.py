from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from uuid import UUID

class HybridSearchRequest(BaseModel):
    query: Optional[str] = None
    vector: Optional[List[float]] = None
    filter_exp: Optional[str] = None
    limit: int = 50
    offset: int = 0

class SearchResultResponse(BaseModel):
    hits: List[Dict[str, Any]]
    total: int
    offset: int
    limit: int
    detected_tags: Optional[List[Dict[str, Any]]] = None
    matched_anchors: Optional[List[Dict[str, Any]]] = None
