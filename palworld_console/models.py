from dataclasses import asdict, dataclass, field
from typing import Any
import uuid


SCHEMA_VERSION = 2


@dataclass(frozen=True)
class TaskProgress:
    percent: int
    stage: str
    message: str = ""
    indeterminate: bool = False

    def __post_init__(self):
        object.__setattr__(self, "percent", max(0, min(100, int(self.percent))))


@dataclass(frozen=True)
class LocalSaveSource:
    source_path: str
    source_kind: str
    savegames_root: str
    world_relative_path: str
    world_id: str
    file_count: int
    total_bytes: int
    has_level: bool
    has_players: bool
    save_format: str
    modified_at: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ServerWorldTarget:
    savegames_path: str
    world_path: str
    world_id: str
    file_count: int
    modified_at: str


@dataclass(frozen=True)
class PlayerIdentityMapping:
    old_guid: str
    new_guid: str
    old_name: str = ""
    new_name: str = ""
    old_instance_id: str = ""
    confirmed: bool = False
    new_instance_id: str = ""
    status: str = "pending"


@dataclass
class CoopMigrationSession:
    instance_id: str
    source_path: str
    target_world_path: str
    phase: str = "source_ready"
    source_players: tuple[dict[str, Any], ...] = ()
    baseline_player_files: tuple[str, ...] = ()
    placeholder_players: tuple[dict[str, Any], ...] = ()
    mappings: tuple[PlayerIdentityMapping, ...] = ()
    backup_path: str = ""
    source_world_hash: str = ""
    package_path: str = ""
    package_hash: str = ""
    target_snapshot_path: str = ""
    target_world_hash: str = ""
    target_kind: str = "local"
    target_platform: str = "windows"
    pending_player_guids: tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class LocalSteamCmdState:
    root: str
    executable: str
    ready: bool
    downloaded: bool = False
    repaired: bool = False
    detail: str = ""


@dataclass(frozen=True)
class RemoteVolume:
    root: str
    total_bytes: int = 0
    free_bytes: int = 0
    writable: bool = False
    recommended: bool = False
    label: str = ""


@dataclass(frozen=True)
class PrerequisiteStatus:
    steamcmd: bool = False
    powershell: bool = False
    systemd: bool = False
    winsw: bool = False
    elevated: bool = False
    download_tool: bool = False
    archive_tool: bool = False
    missing: tuple[str, ...] = ()
    repair_actions: tuple[str, ...] = ()


@dataclass(frozen=True)
class RemotePlatformProfile:
    platform: str = "unknown"
    version: str = ""
    architecture: str = ""
    shell: str = "unknown"
    path_style: str = "unknown"
    service_manager: str = "unknown"
    home_dir: str = ""
    volumes: tuple[RemoteVolume, ...] = ()
    prerequisites: PrerequisiteStatus = field(default_factory=PrerequisiteStatus)


@dataclass(frozen=True)
class PlayerRoleIdentity:
    instance_id: str
    player_uid: str
    user_id: str = ""
    nickname: str = ""
    account_name: str = ""


@dataclass(frozen=True)
class ConfigSyncResult:
    values: dict[str, Any]
    config_path: str
    source: str
    created: bool = False
    synced_at: str = ""


@dataclass(frozen=True)
class UninstallResult:
    install_dir: str
    backup_path: str = ""
    had_saved_data: bool = False


@dataclass(frozen=True)
class EndpointStatus:
    name: str
    protocol: str
    port: int
    listening: bool = False
    reachable: bool | None = None
    detail: str = ""


@dataclass(frozen=True)
class ServerHealthSnapshot:
    healthy: bool
    service_state: str = "unknown"
    pid: int = 0
    process_user: str = ""
    cpu_percent: float = 0.0
    memory_percent: float = 0.0
    memory_mb: float = 0.0
    disk: str = ""
    uptime_seconds: int = 0
    fps: float = 0.0
    frame_time_ms: float = 0.0
    player_count: int = 0
    player_limit: int = 0
    game_days: int = 0
    version: str = ""
    world_guid: str = ""
    game_endpoint: EndpointStatus | None = None
    rest_endpoint: EndpointStatus | None = None
    ssh_ok: bool = False
    rest_ok: bool = False
    ufw_active: bool = False
    ufw_game_allowed: bool = False
    issues: tuple[str, ...] = ()
    recent_log: str = ""
    checked_at: str = ""


@dataclass(frozen=True)
class PlayerRecord:
    name: str = ""
    account_name: str = ""
    user_id: str = ""
    player_uid: str = ""
    level: int = 0
    ping: float = 0.0
    ip: str = ""
    location_x: float = 0.0
    location_y: float = 0.0
    building_count: int = 0
    guild_id: str = ""
    experience: int = 0
    online: bool = False
    first_seen: str = ""
    last_seen: str = ""
    save_status: str = "unknown"
    note: str = ""


@dataclass(frozen=True)
class GuildSummary:
    guild_id: str
    name: str
    member_count: int = 0
    online_count: int = 0
    average_level: float = 0.0
    base_count: int = 0
    pal_count: int = 0
    members: tuple[str, ...] = ()


@dataclass(frozen=True)
class SettingDefinition:
    key: str
    label: str
    category: str
    value_type: str
    default: Any
    minimum: float | None = None
    maximum: float | None = None
    restart_required: bool = True
    description: str = ""


@dataclass(frozen=True)
class BackupArtifact:
    archive_path: str
    sha256: str
    entry_count: int
    size_bytes: int
    created_at: str


@dataclass(frozen=True)
class BackupEntry:
    path: str
    sha256: str
    size_bytes: int
    component: str
    required: bool = False


@dataclass(frozen=True)
class BackupManifest:
    schema: str
    package_id: str
    backup_type: str
    source_instance_id: str
    source_instance_name: str
    source_platform: str
    created_at: str
    world_id: str = ""
    game_version: str = ""
    save_format: str = "unknown"
    components: tuple[str, ...] = ()
    entries: tuple[BackupEntry, ...] = ()
    redacted_fields: tuple[str, ...] = ()
    player_count: int = 0
    incomplete: bool = False
    note: str = ""


@dataclass(frozen=True)
class RestorePlan:
    package_path: str
    source_instance_id: str
    target_instance_id: str
    components: tuple[str, ...]
    cross_instance: bool = False
    version_mismatch: bool = False
    world_mismatch: bool = False
    requires_advanced_confirmation: bool = False
    estimated_bytes: int = 0
    summary: tuple[str, ...] = ()
    blocked_reason: str = ""


@dataclass(frozen=True)
class RestoreResult:
    restored: bool
    package_path: str
    restore_point: str = ""
    components: tuple[str, ...] = ()
    rolled_back: bool = False
    detail: str = ""


@dataclass(frozen=True)
class PlayerProfile:
    player_uid: str
    name: str = ""
    level: int = 0
    experience: int = 0
    guild_id: str = ""
    online_seconds: int = 0
    first_seen: str = ""
    last_seen: str = ""
    note: str = ""


@dataclass(frozen=True)
class PalRecord:
    instance_id: str
    owner_uid: str = ""
    species: str = ""
    nickname: str = ""
    level: int = 1
    experience: int = 0
    gender: str = ""
    passive_skills: tuple[str, ...] = ()
    active_skills: tuple[str, ...] = ()
    container_id: str = ""


@dataclass(frozen=True)
class InventoryRecord:
    container_id: str
    slot: int
    item_id: str = ""
    quantity: int = 0
    owner_uid: str = ""
    category: str = "inventory"


@dataclass(frozen=True)
class GuildRecord:
    guild_id: str
    name: str = ""
    leader_uid: str = ""
    member_uids: tuple[str, ...] = ()
    base_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class BaseRecord:
    base_id: str
    guild_id: str = ""
    name: str = ""
    worker_pal_ids: tuple[str, ...] = ()
    container_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScheduleDefinition:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = "计划任务"
    action: str = "backup"
    schedule: str = "04:00"
    enabled: bool = False
    retention: int = 14
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WhitelistEntry:
    player_uid: str
    player_name: str = ""
    platform: str = "Unknown"
    note: str = ""
    enabled: bool = True


@dataclass(frozen=True)
class SaveChangeSet:
    operation: str
    target_type: str
    target_id: str
    changes: dict[str, Any] = field(default_factory=dict)
    reason: str = ""


@dataclass(frozen=True)
class SaveValidationResult:
    valid: bool
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    affected: tuple[str, ...] = ()


@dataclass
class ServerInstance:
    schema_version: int = SCHEMA_VERSION
    name: str = "本机服务器"
    kind: str = "local"  # local | remote
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    install_dir: str = ""
    host: str = "127.0.0.1"
    remote_username: str = ""
    ssh_port: int = 22
    game_port: int = 8211
    rest_url: str = ""
    public_address: str = ""
    admin_secret_ref: str = ""
    server_password_secret_ref: str = ""
    ssh_secret_ref: str = ""
    ssh_auth_type: str = "password"
    ssh_key_path: str = ""
    ssh_key_passphrase_ref: str = ""
    remote_profile: dict[str, Any] = field(default_factory=dict)
    discovery_status: str = "not_checked"
    discovered_at: str = ""
    user_overrides: list[str] = field(default_factory=list)
    last_status: str = "unknown"
    last_backup: str = ""
    config_source: str = ""
    config_synced_at: str = ""
    config_restart_required: bool = False
    config_cache_state: dict[str, Any] = field(default_factory=dict)
    operation_history: list[dict[str, Any]] = field(default_factory=list)
    last_diagnostic: dict[str, Any] = field(default_factory=dict)
    rcon_port: int = 25575
    rcon_enabled: bool = False
    rcon_secret_ref: str = ""
    schedules: list[dict[str, Any]] = field(default_factory=list)
    whitelist: list[dict[str, Any]] = field(default_factory=list)
    whitelist_policy: str = "log"
    player_history: dict[str, dict[str, Any]] = field(default_factory=dict)
    mods: list[dict[str, Any]] = field(default_factory=list)
    mod_environment: dict[str, Any] = field(default_factory=dict)
    mod_profile: dict[str, Any] = field(default_factory=dict)
    mod_last_sync: str = ""
    workshop_catalog_state: dict[str, Any] = field(default_factory=dict)
    mod_source_preference: str = "workshop"
    local_steamcmd_state: dict[str, Any] = field(default_factory=dict)
    mod_environment_version: int = 1
    wine_migration: dict[str, Any] = field(default_factory=dict)
    ui_preferences: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ServerInstance":
        valid = {f.name for f in cls.__dataclass_fields__.values()}
        values = {k: v for k, v in data.items() if k in valid}
        profile = values.get("remote_profile")
        if values.get("kind") == "remote" and isinstance(profile, dict) and profile and "platform" not in profile:
            values["remote_profile"] = {**profile, "platform": "linux", "platform_inferred": True}
        return cls(**values)
