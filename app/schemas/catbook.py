from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class ErrorResponse(BaseModel):
    error_code: str = Field(..., description="Machine-readable error code.")
    detail: str = Field(..., description="Human-readable error message.")


class PostCreate(BaseModel):
    post_id: str
    author_id: str
    player_name: Optional[str] = None
    title: str
    content: str
    image_id: Optional[str] = None
    image_url: Optional[str] = None
    post_type: Optional[str] = None


class PostAck(BaseModel):
    post_id: str
    server_created_at: int = Field(..., description="UTC milliseconds since epoch.")


class CommentCreate(BaseModel):
    comment_id: str
    post_id: str
    author_id: str
    player_name: Optional[str] = None
    content: str
    parent_comment_id: Optional[str] = None


class CommentAck(BaseModel):
    comment_id: str
    server_created_at: int = Field(..., description="UTC milliseconds since epoch.")


class InteractionRequest(BaseModel):
    post_id: str
    actor_global_id: str


class InteractionAck(BaseModel):
    post_id: str
    actor_global_id: str
    server_created_at: int = Field(..., description="UTC milliseconds since epoch.")


class PostItem(BaseModel):
    post_id: str
    author_id: str
    player_name: Optional[str] = None
    title: str
    content: str
    image_id: Optional[str] = None
    image_url: Optional[str] = None
    post_type: str = "life"
    server_created_at: int = Field(..., description="UTC milliseconds since epoch.")


class CommentItem(BaseModel):
    comment_id: str
    post_id: str
    author_id: str
    player_name: Optional[str] = None
    content: str
    parent_comment_id: Optional[str] = None
    server_created_at: int = Field(..., description="UTC milliseconds since epoch.")


class FeedResponse(BaseModel):
    posts: List[PostItem] = Field(default_factory=list)
    cursor: Optional[str] = None
    has_more: bool = False


class CommentsResponse(BaseModel):
    comments: List[CommentItem] = Field(default_factory=list)


class SyncItem(BaseModel):
    kind: Literal["post", "comment", "like", "bookmark"]
    entity_id: str
    payload: Dict[str, Any]


class SyncRequest(BaseModel):
    items: List[SyncItem]


class SyncAck(BaseModel):
    entity_id: str
    ok: bool
    server_created_at: Optional[int] = None
    error_code: Optional[str] = None


class SyncResponse(BaseModel):
    acks: List[SyncAck] = Field(default_factory=list)
