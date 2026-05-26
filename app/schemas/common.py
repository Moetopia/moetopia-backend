from pydantic import BaseModel
from typing import Generic, TypeVar, Optional, Dict, Any

T = TypeVar("T")

class ResponseBase(BaseModel, Generic[T]):
    code: int = 200
    message: str = "success"
    data: Optional[T] = None
    extra: Optional[Dict[str, Any]] = None

class PaginatedData(BaseModel, Generic[T]):
    items: list[T]
    total: int
    has_more: bool
