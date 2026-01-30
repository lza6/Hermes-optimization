from pydantic import BaseModel, Field
from typing import List, Optional, Union, Dict, Any

# Chat Completion Schemas
class ChatMessageContentItem(BaseModel):
    type: str # text or image_url
    text: Optional[str] = None
    image_url: Optional[Dict[str, Any]] = None

class ChatMessage(BaseModel):
    role: str
    content: Union[str, List[ChatMessageContentItem]]

class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[ChatMessage]
    stream: Optional[bool] = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    # Add other OpenAI compatible fields as necessary
    top_p: Optional[float] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None

# Provider Schemas
class ProviderBase(BaseModel):
    name: str = Field(..., description="Provider Name (提供商名称)")
    baseUrl: str = Field(..., description="Base URL (基础地址)")
    apiKey: str = Field(..., description="API Key (密钥)")
    modelBlacklist: Optional[List[str]] = Field(default=[], description="Model Blacklist (模型黑名单)")

class ProviderCreate(ProviderBase):
    pass

class ProviderUpdate(BaseModel):
    name: Optional[str] = None
    baseUrl: Optional[str] = None
    apiKey: Optional[str] = None
    modelBlacklist: Optional[List[str]] = None

class ProviderResponse(ProviderBase):
    id: str
    models: List[str] = []
    status: str
    lastSyncedAt: Optional[int] = None
    lastUsedAt: Optional[int] = None
    createdAt: int

class ProviderImportItem(ProviderBase):
    pass

class ProviderImportRequest(BaseModel):
    providers: List[ProviderImportItem]

# Settings Schemas
class PeriodicSyncIntervalRequest(BaseModel):
    intervalHours: float

class ChatMaxRetriesRequest(BaseModel):
    maxRetries: int

class DispatcherSettingsRequest(BaseModel):
    initialPenaltyMs: Optional[int] = None
    maxPenaltyMs: Optional[int] = None
    resyncThreshold: Optional[int] = None
    resyncCooldownMs: Optional[int] = None

class ClearCooldownRequest(BaseModel):
    providerId: str
    modelName: str

# Key Management Schemas
class KeyGenerateRequest(BaseModel):
    description: Optional[str] = None
    key: Optional[str] = None

class KeyResponse(BaseModel):
    id: str
    key: str # Only returned on creation usually, or check logic
    description: Optional[str] = None

class KeyInfo(BaseModel):
    id: str
    key_hash: str
    description: Optional[str]
    createdAt: int
    lastUsedAt: Optional[int]
