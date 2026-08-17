from pydantic import BaseModel, HttpUrl, EmailStr
from typing import Optional, Literal


class SubmitJobRequest(BaseModel):
    url: HttpUrl


class SubmitJobResponse(BaseModel):
    job_id: str


class JobMetadata(BaseModel):
    title: Optional[str] = None
    uploader: Optional[str] = None
    duration: Optional[int] = None
    source_url: Optional[str] = None


class JobResult(BaseModel):
    metadata: JobMetadata
    original_transcript: str
    hindi: str
    english: str
    hinglish: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: Literal["pending", "started", "success", "failure"]
    result: Optional[JobResult] = None
    error: Optional[str] = None


class SignupRequest(BaseModel):
    email: EmailStr
    password: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserResponse(BaseModel):
    id: str
    email: str


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse