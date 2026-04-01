"""
Pydantic models for request/response schemas
"""
from pydantic import BaseModel, Field, field_validator, model_validator
from typing import List, Optional, Dict
import ipaddress
import re


def validate_ip_address(ip: str) -> str:
    """Validate IP address format (with optional port)"""
    # Handle IPv4:port format (e.g., 192.168.1.1:5555)
    ip_only = ip
    if ':' in ip:
        ip_only, port = ip.rsplit(':', 1)
        if not port.isdigit() or not (1 <= int(port) <= 65535):
            raise ValueError(f"Invalid port in IP address: {ip}")
    try:
        ipaddress.ip_address(ip_only)
        return ip
    except ValueError:
        raise ValueError(f"Invalid IP address: {ip}")


PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$")


def validate_ntp_server(value: str) -> str:
    """Validate NTP server as hostname or IP address."""
    server = (value or "").strip()
    if not server:
        raise ValueError("NTP server is required")
    if len(server) > 255:
        raise ValueError("NTP server is too long")

    try:
        ipaddress.ip_address(server)
        return server
    except ValueError:
        pass

    normalized = server[:-1] if server.endswith('.') else server
    labels = normalized.split('.')
    if not labels or any(not label for label in labels):
        raise ValueError("Invalid NTP server")

    label_re = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?$")
    for label in labels:
        if not label_re.fullmatch(label):
            raise ValueError("Invalid NTP server")
    return normalized


# ============================================
# Device Models
# ============================================

class DeviceActionRequest(BaseModel):
    ips: List[str]
    mode: str

    @field_validator('ips')
    @classmethod
    def validate_ips(cls, v):
        return [validate_ip_address(ip) for ip in v]


class RenameRequest(BaseModel):
    ips: List[str]
    new_name: str

    @field_validator('ips')
    @classmethod
    def validate_ips(cls, v):
        return [validate_ip_address(ip) for ip in v]

    @field_validator('new_name')
    @classmethod
    def validate_name(cls, v):
        # Only allow alphanumeric, dash, underscore, space
        if not re.match(r'^[\w\s\-]+$', v):
            raise ValueError('Device name can only contain letters, numbers, spaces, dashes and underscores')
        if len(v) > 32:
            raise ValueError('Device name must be 32 characters or less')
        return v


class PingRequest(BaseModel):
    ips: List[str]

    @field_validator('ips')
    @classmethod
    def validate_ips(cls, v):
        return [validate_ip_address(ip) for ip in v]


class DeviceData(BaseModel):
    """Device data model for CRUD operations"""
    Asset_Name: Optional[str] = None
    Asset_Tag: Optional[str] = None
    IP: Optional[str] = None
    MAC_Address: Optional[str] = None
    Model: Optional[str] = None
    Category: Optional[str] = None
    Manufacturer: Optional[str] = None
    Serial: Optional[str] = None
    Default_Location: Optional[str] = None
    Project: Optional[str] = None
    Work_Center: Optional[str] = None
    Monotor: Optional[str] = None

    class Config:
        extra = "allow"


class DeviceImportRequest(BaseModel):
    devices: List[Dict]


PLANT_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")


# ============================================
# App Models
# ============================================

class AppRequest(BaseModel):
    ips: List[str]
    package: str = "asd.kce.machinemonitor"

    @field_validator('ips')
    @classmethod
    def validate_ips(cls, v):
        return [validate_ip_address(ip) for ip in v]

    @field_validator('package')
    @classmethod
    def validate_package(cls, v):
        if not PACKAGE_RE.fullmatch(v):
            raise ValueError('Invalid Android package name')
        return v


class PlantRequest(BaseModel):
    name: str
    code: Optional[str] = None
    location: Optional[str] = ""
    timezone: Optional[str] = "Asia/Bangkok"
    description: Optional[str] = ""
    is_active: Optional[bool] = True

    @field_validator('name')
    @classmethod
    def validate_name(cls, v):
        value = (v or "").strip()
        if not value:
            raise ValueError("Plant name is required")
        if len(value) > 100:
            raise ValueError("Plant name must be 100 characters or less")
        return value

    @field_validator('code')
    @classmethod
    def validate_code(cls, v):
        if v is None or not v.strip():
            return None
        value = v.strip()
        if not PLANT_CODE_RE.fullmatch(value):
            raise ValueError("Plant code can only contain letters, numbers, dashes and underscores")
        return value

    @field_validator('location', 'timezone', 'description')
    @classmethod
    def validate_text_fields(cls, v):
        if v is None:
            return ""
        value = v.strip()
        if len(value) > 255:
            raise ValueError("Field is too long")
        return value


class PlantUpdateRequest(BaseModel):
    name: Optional[str] = None
    code: Optional[str] = None
    location: Optional[str] = None
    timezone: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None

    @field_validator('name')
    @classmethod
    def validate_name(cls, v):
        if v is None:
            return None
        value = v.strip()
        if not value:
            raise ValueError("Plant name cannot be empty")
        if len(value) > 100:
            raise ValueError("Plant name must be 100 characters or less")
        return value

    @field_validator('code')
    @classmethod
    def validate_code(cls, v):
        if v is None:
            return None
        value = v.strip()
        if not value:
            raise ValueError("Plant code cannot be empty")
        if not PLANT_CODE_RE.fullmatch(value):
            raise ValueError("Plant code can only contain letters, numbers, dashes and underscores")
        return value

    @field_validator('location', 'timezone', 'description')
    @classmethod
    def validate_text_fields(cls, v):
        if v is None:
            return None
        value = v.strip()
        if len(value) > 255:
            raise ValueError("Field is too long")
        return value


# ============================================
# Auth Models
# ============================================

class LoginRequest(BaseModel):
    username: str
    password: str
    remember: bool = False


class UserRequest(BaseModel):
    username: str
    password: str = ""
    role: str = "user"
    plant_code: Optional[str] = None


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


# ============================================
# Report Models
# ============================================

class DeviceReportItem(BaseModel):
    """Device item for report generation - validates required fields"""
    IP: str
    Asset_Name: Optional[str] = None
    Default_Location: Optional[str] = None
    Work_Center: Optional[str] = None
    Model: Optional[str] = None

    @field_validator('IP')
    @classmethod
    def validate_ip(cls, v):
        return validate_ip_address(v)

    class Config:
        extra = "allow"  # Allow additional fields


class GenerateReportRequest(BaseModel):
    """Request model for report generation"""
    devices: List[DeviceReportItem]


class ReportRequest(BaseModel):
    date: str
    total: int
    online: int
    offline: int
    devices: List[Dict]


class ReportEmailRequest(BaseModel):
    total: int
    online: int
    offline: int
    devices: List[Dict]
    template_id: Optional[str] = None


class ReportTemplateSettings(BaseModel):
    """Report template settings"""
    title: Optional[str] = "Daily TV Status Report"
    company: Optional[str] = "THE-Corp"
    department: Optional[str] = "IT Department"
    document_no: Optional[str] = "RPT-001"
    version: Optional[str] = "1.0"
    prepared_by: Optional[str] = "IT Admin"


class ReportTemplateRequest(BaseModel):
    """Report template data model"""
    name: str
    description: Optional[str] = ""
    elements: List[Dict]
    settings: Optional[Dict] = None


# ============================================
# Remote Control Models
# ============================================

class TapRequest(BaseModel):
    ip: str
    x: int
    y: int

    @field_validator('ip')
    @classmethod
    def validate_ip(cls, v):
        return validate_ip_address(v)

    @field_validator('x', 'y')
    @classmethod
    def validate_coordinates(cls, v):
        if v < 0:
            raise ValueError('Coordinates must be non-negative')
        return v


class SwipeRequest(BaseModel):
    ip: str
    x1: int
    y1: int
    x2: int
    y2: int
    duration: int = 300

    @field_validator('ip')
    @classmethod
    def validate_ip(cls, v):
        return validate_ip_address(v)

    @field_validator('x1', 'y1', 'x2', 'y2')
    @classmethod
    def validate_coordinates(cls, v):
        if v < 0:
            raise ValueError('Coordinates must be non-negative')
        return v

    @field_validator('duration')
    @classmethod
    def validate_duration(cls, v):
        if not (0 <= v <= 60000):
            raise ValueError('Duration must be between 0 and 60000 ms')
        return v


class KeyRequest(BaseModel):
    ip: str
    keycode: int

    @field_validator('ip')
    @classmethod
    def validate_ip(cls, v):
        return validate_ip_address(v)

    @field_validator('keycode')
    @classmethod
    def validate_keycode(cls, v):
        if not (0 <= v <= 9999):
            raise ValueError('Invalid keycode')
        return v


class TextRequest(BaseModel):
    ip: str
    text: str

    @field_validator('ip')
    @classmethod
    def validate_ip(cls, v):
        return validate_ip_address(v)

    @field_validator('text')
    @classmethod
    def validate_text(cls, v):
        if not v:
            raise ValueError('Text is required')
        if len(v) > 200:
            raise ValueError('Text must be 200 characters or less')
        if any(ord(ch) < 32 for ch in v):
            raise ValueError('Control characters are not allowed')
        if any(ch in v for ch in ['"', "'", '`', ';', '&', '|', '<', '>', '$']):
            raise ValueError('Text contains unsupported characters')
        return v


# ============================================
# Automation Models
# ============================================

AUTOMATION_TRIGGER_TYPES = {
    "schedule",
    "event_app_stopped",
    "event_offline",
    "event_high_cache",
}

AUTOMATION_CONDITION_TYPES = {
    "consecutive_failures_gt",
    "cache_gt",
    "ram_lt",
    "storage_lt",
}

AUTOMATION_ACTION_TYPES = {
    "restart_app",
    "reboot_device",
    "clear_cache",
    "clear_app_data",
    "send_email",
    "send_syno_chat",
    "run_health_check",
    "wake_device",
    "sleep_device",
    "send_daily_report",
}


def _validate_workflow_name(value: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        raise ValueError("Workflow name is required")
    if len(normalized) > 100:
        raise ValueError("Workflow name must be 100 characters or less")
    return normalized


def _validate_workflow_description(value: str) -> str:
    normalized = (value or "").strip()
    if len(normalized) > 500:
        raise ValueError("Workflow description must be 500 characters or less")
    return normalized


def _validate_workflow_cooldown(value: int) -> int:
    if not (1 <= value <= 1440):
        raise ValueError("Cooldown must be between 1 and 1440 minutes")
    return value


class WorkflowDeviceScope(BaseModel):
    mode: str = "all"  # "all", "selected", or "plant"
    ips: List[str] = Field(default_factory=list)
    plant_id: Optional[str] = None

    @field_validator('mode')
    @classmethod
    def validate_mode(cls, v):
        if v not in {'all', 'selected', 'plant'}:
            raise ValueError("Device scope mode must be 'all', 'selected', or 'plant'")
        return v

    @field_validator('ips')
    @classmethod
    def validate_ips(cls, v):
        return [validate_ip_address(ip) for ip in v]

    @field_validator('plant_id')
    @classmethod
    def validate_plant_id(cls, v):
        if v is None:
            return None
        normalized = str(v).strip()
        if not normalized:
            return None
        if len(normalized) > 50:
            raise ValueError("Plant ID must be 50 characters or less")
        if not re.fullmatch(r'^[A-Za-z0-9_.-]+$', normalized):
            raise ValueError("Plant ID contains unsupported characters")
        return normalized

    @model_validator(mode='after')
    def validate_scope(self):
        if self.mode == 'selected' and not self.ips:
            raise ValueError("Selected device scope must include at least one IP")
        if self.mode == 'plant' and not self.plant_id:
            raise ValueError("Plant scope must include a plant")
        if self.mode == 'all':
            self.ips = []
            self.plant_id = None
        elif self.mode == 'selected':
            self.plant_id = None
        elif self.mode == 'plant':
            self.ips = []
        return self


class WorkflowNode(BaseModel):
    id: str
    category: str  # "trigger", "condition", "action"
    type: str
    config: Dict = Field(default_factory=dict)
    connections: List[str] = Field(default_factory=list)

    @field_validator('id')
    @classmethod
    def validate_id(cls, v):
        value = (v or "").strip()
        if not value:
            raise ValueError("Workflow node id is required")
        if len(value) > 64:
            raise ValueError("Workflow node id is too long")
        return value

    @field_validator('category')
    @classmethod
    def validate_category(cls, v):
        if v not in {'trigger', 'condition', 'action'}:
            raise ValueError("Workflow node category must be trigger, condition, or action")
        return v

    @model_validator(mode='after')
    def validate_type_and_config(self):
        allowed_types = {
            'trigger': AUTOMATION_TRIGGER_TYPES,
            'condition': AUTOMATION_CONDITION_TYPES,
            'action': AUTOMATION_ACTION_TYPES,
        }

        if self.type not in allowed_types[self.category]:
            raise ValueError(f"Unsupported {self.category} node type: {self.type}")

        config = self.config or {}

        if self.type == 'schedule':
            mode = config.get('mode', 'interval')
            if mode not in {'interval', 'daily_time'}:
                raise ValueError("Schedule mode must be 'interval' or 'daily_time'")
            if mode == 'interval':
                interval = config.get('interval', 60)
                try:
                    interval = int(interval)
                except (TypeError, ValueError):
                    raise ValueError("Schedule interval must be an integer")
                if interval < 1:
                    raise ValueError("Schedule interval must be at least 1")
                unit = config.get('unit', 'minutes')
                if unit not in {'minutes', 'hours'}:
                    raise ValueError("Schedule unit must be minutes or hours")
            else:
                schedule_time = str(config.get('time', '08:00'))
                if not re.fullmatch(r'^\d{2}:\d{2}$', schedule_time):
                    raise ValueError("Schedule time must be HH:MM")

        elif self.type in {'event_high_cache'}:
            threshold = config.get('threshold_mb', 100)
            try:
                threshold = float(threshold)
            except (TypeError, ValueError):
                raise ValueError("Threshold must be numeric")
            if threshold <= 0:
                raise ValueError("Threshold must be greater than 0")

        elif self.type in {'consecutive_failures_gt', 'cache_gt', 'ram_lt', 'storage_lt'}:
            value = config.get('value', 0)
            try:
                value = float(value)
            except (TypeError, ValueError):
                raise ValueError("Condition value must be numeric")
            if value <= 0:
                raise ValueError("Condition value must be greater than 0")

        elif self.type in {'restart_app', 'clear_cache', 'clear_app_data'}:
            package = config.get('package', '')
            if package and not PACKAGE_RE.fullmatch(package):
                raise ValueError("Invalid Android package name")

        elif self.type == 'send_email':
            subject = str(config.get('subject', ''))
            if len(subject) > 200:
                raise ValueError("Email subject must be 200 characters or less")

        elif self.type == 'send_syno_chat':
            message = str(config.get('message', ''))
            if len(message) > 1000:
                raise ValueError("Chat message must be 1000 characters or less")

        return self


class CreateWorkflowRequest(BaseModel):
    name: str
    description: str = ""
    device_scope: WorkflowDeviceScope
    nodes: List[WorkflowNode]
    cooldown_minutes: int = 5

    @field_validator('name')
    @classmethod
    def validate_name(cls, v):
        return _validate_workflow_name(v)

    @field_validator('description')
    @classmethod
    def validate_description(cls, v):
        return _validate_workflow_description(v)

    @field_validator('cooldown_minutes')
    @classmethod
    def validate_cooldown(cls, v):
        return _validate_workflow_cooldown(v)

    @model_validator(mode='after')
    def validate_nodes(self):
        triggers = [n for n in self.nodes if n.category == 'trigger']
        actions = [n for n in self.nodes if n.category == 'action']
        if not triggers:
            raise ValueError("Workflow must include at least one trigger")
        if not actions:
            raise ValueError("Workflow must include at least one action")
        return self


class UpdateWorkflowRequest(BaseModel):
    name: str
    description: str = ""
    device_scope: WorkflowDeviceScope
    nodes: List[WorkflowNode]
    cooldown_minutes: int = 5

    @field_validator('name')
    @classmethod
    def validate_name(cls, v):
        return _validate_workflow_name(v)

    @field_validator('description')
    @classmethod
    def validate_description(cls, v):
        return _validate_workflow_description(v)

    @field_validator('cooldown_minutes')
    @classmethod
    def validate_cooldown(cls, v):
        return _validate_workflow_cooldown(v)

    @model_validator(mode='after')
    def validate_nodes(self):
        triggers = [n for n in self.nodes if n.category == 'trigger']
        actions = [n for n in self.nodes if n.category == 'action']
        if not triggers:
            raise ValueError("Workflow must include at least one trigger")
        if not actions:
            raise ValueError("Workflow must include at least one action")
        return self
