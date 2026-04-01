"""
Automation Engine - Background service that evaluates workflows
and executes actions based on device status triggers.
"""
import asyncio
import time
import logging
from typing import Dict, List, Optional
from datetime import datetime, timedelta, date

from config import DEFAULT_APP_PACKAGE
from ntp_service import ntp_service
from websocket_manager import ws_manager

logger = logging.getLogger(__name__)

# Actions that operate at workflow level (run once), not per-device
WORKFLOW_LEVEL_ACTIONS = {'send_daily_report'}


class AutomationEngine:
    """
    Background service that evaluates automation workflows.
    Integrates with ConnectionChecker for event-based triggers
    and with ADBManager/AlertManager for actions.
    """

    def __init__(self):
        self._running = False
        self._scheduler_task: Optional[asyncio.Task] = None
        self._workflows_cache: Dict[str, Dict] = {}
        # Cooldown: key = "workflow_id:device_ip", value = datetime of last execution
        self._cooldowns: Dict[str, datetime] = {}
        self._daily_schedule_runs: Dict[str, date] = {}
        self._lock = asyncio.Lock()
        # Track previous device states for edge detection
        self._prev_app_status: Dict[str, str] = {}  # ip -> "RUNNING"/"STOPPED"
        self._prev_online: Dict[str, bool] = {}  # ip -> True/False
        self._last_evaluation: Optional[datetime] = None
        self._last_cleanup: Optional[datetime] = None

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self):
        """Start the automation engine"""
        if self._running:
            logger.warning("[AutomationEngine] Already running")
            return

        self._running = True
        await self.reload_workflows()
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        logger.info(f"[AutomationEngine] Started with {len(self._workflows_cache)} workflows")

    async def stop(self):
        """Stop the automation engine"""
        self._running = False
        if self._scheduler_task:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
        logger.info("[AutomationEngine] Stopped")

    async def reload_workflows(self):
        """Reload all enabled workflows from database"""
        from database import get_all_workflows
        try:
            all_wf = await asyncio.to_thread(get_all_workflows)
            async with self._lock:
                self._workflows_cache = {
                    wf['id']: wf for wf in all_wf if wf.get('enabled')
                }
                active_ids = set(self._workflows_cache.keys())
                self._cooldowns = {
                    key: value
                    for key, value in self._cooldowns.items()
                    if key.split(':', 1)[0] in active_ids
                }
                self._daily_schedule_runs = {
                    key: value
                    for key, value in self._daily_schedule_runs.items()
                    if key.split(':', 1)[0] in active_ids
                }
            logger.info(f"[AutomationEngine] Loaded {len(self._workflows_cache)} enabled workflows")
        except Exception as e:
            logger.error(f"[AutomationEngine] Failed to reload workflows: {e}")

    async def reset_workflow_state(self, workflow_id: str):
        """Clear in-memory runtime state for a workflow after edits/toggles."""
        async with self._lock:
            self._cooldowns = {
                key: value
                for key, value in self._cooldowns.items()
                if not key.startswith(f"{workflow_id}:")
            }
            self._daily_schedule_runs = {
                key: value
                for key, value in self._daily_schedule_runs.items()
                if not key.startswith(f"{workflow_id}:")
            }
            if workflow_id in self._workflows_cache:
                self._workflows_cache[workflow_id]['last_triggered_at'] = None

    def _now(self) -> datetime:
        """Current webapp time adjusted by NTP offset when available."""
        return ntp_service.now()

    def _daily_schedule_key(self, workflow_id: str, trigger_id: str, target_time: str) -> str:
        return f"{workflow_id}:{trigger_id}:{target_time}"

    def _has_daily_schedule_run_today(
        self,
        workflow_id: str,
        trigger_id: str,
        target_time: str,
        target_minutes: int,
        last_triggered_at: Optional[datetime],
        now: datetime,
    ) -> bool:
        key = self._daily_schedule_key(workflow_id, trigger_id, target_time)
        if self._daily_schedule_runs.get(key) == now.date():
            return True

        if last_triggered_at and last_triggered_at.date() == now.date():
            last_minutes = last_triggered_at.hour * 60 + last_triggered_at.minute
            if abs(last_minutes - target_minutes) <= 2:
                self._daily_schedule_runs[key] = now.date()
                return True

        return False

    def _mark_daily_schedule_run(self, workflow_id: str, trigger_id: str, target_time: str, when: datetime):
        self._daily_schedule_runs[self._daily_schedule_key(workflow_id, trigger_id, target_time)] = when.date()

    # ========================================
    # Scheduler Loop (for schedule triggers)
    # ========================================

    async def _scheduler_loop(self):
        """Check schedule-based triggers every 30 seconds"""
        while self._running:
            try:
                await self._evaluate_schedule_triggers()
                await self._maybe_cleanup_logs()
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[AutomationEngine] Scheduler error: {e}")
                await asyncio.sleep(10)

    async def _maybe_cleanup_logs(self):
        """Run log cleanup once per day"""
        now = self._now()
        if self._last_cleanup and (now - self._last_cleanup).total_seconds() < 86400:
            return
        try:
            from database import cleanup_automation_logs
            deleted = await asyncio.to_thread(cleanup_automation_logs, 30)
            self._last_cleanup = now
            if deleted > 0:
                logger.info(f"[AutomationEngine] Cleaned up {deleted} logs older than 30 days")
        except Exception as e:
            logger.error(f"[AutomationEngine] Log cleanup error: {e}")

    async def _evaluate_schedule_triggers(self):
        """Evaluate all schedule-type triggers"""
        async with self._lock:
            workflows = list(self._workflows_cache.values())

        for wf in workflows:
            trigger_nodes = [n for n in wf.get('nodes', []) if n.get('category') == 'trigger' and n.get('type') == 'schedule']
            if not trigger_nodes:
                continue

            for trigger in trigger_nodes:
                config = trigger.get('config', {})
                schedule_mode = config.get('mode', 'interval')
                trigger_id = trigger.get('id', 'schedule')

                # Parse last_triggered_at
                last = wf.get('last_triggered_at')
                if last and isinstance(last, str):
                    try:
                        last = datetime.fromisoformat(last)
                    except (ValueError, TypeError):
                        last = None

                if schedule_mode == 'daily_time':
                    # Daily at specific time - check if current time matches
                    target_time = config.get('time', '08:00')
                    try:
                        t_hour, t_min = map(int, target_time.split(':'))
                    except (ValueError, TypeError):
                        t_hour, t_min = 8, 0

                    now = self._now()
                    # Check if we're within the time window (±2 minutes)
                    now_minutes = now.hour * 60 + now.minute
                    target_minutes = t_hour * 60 + t_min
                    if abs(now_minutes - target_minutes) > 2:
                        continue

                    # Check if this specific daily schedule already ran today
                    if self._has_daily_schedule_run_today(wf['id'], trigger_id, target_time, target_minutes, last, now):
                        continue
                else:
                    # Interval mode (original behavior)
                    interval_min = config.get('interval', 60)
                    unit = config.get('unit', 'minutes')
                    if unit == 'hours':
                        interval_min *= 60

                    if last and (self._now() - last).total_seconds() < interval_min * 60:
                        continue

                # Build trigger context based on schedule mode
                if schedule_mode == 'daily_time':
                    trigger_ctx = {"mode": "daily_time", "time": config.get('time', '08:00')}
                else:
                    trigger_ctx = {"mode": "interval", "interval": interval_min, "unit": unit}

                # Check if workflow has only workflow-level actions (e.g. daily report)
                action_types = {n.get('type') for n in wf.get('nodes', []) if n.get('category') == 'action'}
                only_workflow_level = action_types and action_types.issubset(WORKFLOW_LEVEL_ACTIONS)

                if only_workflow_level:
                    # Execute once for the entire workflow (not per device)
                    if not self._check_cooldown(wf['id'], '__workflow__', wf.get('cooldown_minutes', 5)):
                        continue
                    await self._execute_workflow(
                        wf, 'all devices', 'schedule', trigger_ctx
                    )
                    if schedule_mode == 'daily_time':
                        self._mark_daily_schedule_run(wf['id'], trigger_id, config.get('time', '08:00'), self._now())
                else:
                    # Per-device execution
                    device_ips = await self._get_workflow_devices(wf)
                    for ip in device_ips:
                        if not self._check_cooldown(wf['id'], ip, wf.get('cooldown_minutes', 5)):
                            continue
                        await self._execute_workflow(
                            wf, ip, 'schedule', trigger_ctx
                        )
                        if schedule_mode == 'daily_time':
                            self._mark_daily_schedule_run(wf['id'], trigger_id, config.get('time', '08:00'), self._now())

    # ========================================
    # Event Evaluation (called by connection_checker)
    # ========================================

    async def evaluate_events(self, status_cache: Dict):
        """
        Called after each connection_checker cycle.
        Evaluates event triggers against current device statuses.
        status_cache: Dict[ip, DeviceStatus dataclass]
        """
        if not self._running:
            return

        self._last_evaluation = self._now()

        async with self._lock:
            workflows = list(self._workflows_cache.values())

        if not workflows:
            return

        for wf in workflows:
            trigger_nodes = [n for n in wf.get('nodes', [])
                            if n.get('category') == 'trigger' and n.get('type') != 'schedule']
            if not trigger_nodes:
                continue

            # Check if workflow has only workflow-level actions
            action_types = {n.get('type') for n in wf.get('nodes', []) if n.get('category') == 'action'}
            only_workflow_level = action_types and action_types.issubset(WORKFLOW_LEVEL_ACTIONS)

            device_ips = await self._get_workflow_devices(wf)

            if only_workflow_level:
                # For workflow-level actions, check if ANY device triggered, then execute once
                any_triggered = False
                trigger_ctx = {}
                for ip in device_ips:
                    status = status_cache.get(ip)
                    if not status:
                        continue
                    for trigger in trigger_nodes:
                        if self._check_event_trigger(trigger, ip, status):
                            any_triggered = True
                            trigger_ctx = self._build_trigger_context(trigger, ip, status)
                            break
                    if any_triggered:
                        break

                if any_triggered:
                    if self._check_cooldown(wf['id'], '__workflow__', wf.get('cooldown_minutes', 5)):
                        await self._execute_workflow(
                            wf, 'all devices', trigger_ctx.get('trigger_type', 'event'),
                            trigger_ctx
                        )
            else:
                # Per-device execution
                for ip in device_ips:
                    status = status_cache.get(ip)
                    if not status:
                        continue

                    for trigger in trigger_nodes:
                        triggered = self._check_event_trigger(trigger, ip, status)
                        if triggered:
                            if not self._check_cooldown(wf['id'], ip, wf.get('cooldown_minutes', 5)):
                                continue
                            await self._execute_workflow(
                                wf, ip, trigger.get('type', 'unknown'),
                                self._build_trigger_context(trigger, ip, status)
                            )

        # Update previous states for edge detection
        for ip, status in status_cache.items():
            self._prev_app_status[ip] = getattr(status, 'app_status', 'unknown')
            self._prev_online[ip] = getattr(status, 'online', False)

    def _check_event_trigger(self, trigger: Dict, ip: str, status) -> bool:
        """Check if an event trigger matches current status (edge detection)"""
        trigger_type = trigger.get('type', '')
        config = trigger.get('config', {})

        if trigger_type == 'event_app_stopped':
            # Trigger whenever app is STOPPED (cooldown prevents repeated execution)
            current = getattr(status, 'app_status', 'unknown')
            return current == 'STOPPED'

        elif trigger_type == 'event_offline':
            # Edge: was online, now offline (skip if no previous state)
            if ip not in self._prev_online:
                return False
            prev = self._prev_online[ip]
            current = getattr(status, 'online', False)
            return prev and not current

        elif trigger_type == 'event_high_cache':
            threshold = config.get('threshold_mb', 100)
            return getattr(status, 'online', False) and getattr(status, 'cache_mb', 0) > threshold

        elif trigger_type == 'event_low_ram':
            # Low RAM requires a health check, use cached data if available
            threshold = config.get('threshold_mb', 300)
            # We don't have RAM in status_cache from connection_checker
            # This trigger will work with schedule-based health checks
            return False

        return False

    def _build_trigger_context(self, trigger: Dict, ip: str, status) -> Dict:
        """Build context info for the trigger event"""
        context = {
            "ip": ip,
            "trigger_type": trigger.get('type', ''),
            "online": getattr(status, 'online', False),
            "app_status": getattr(status, 'app_status', 'unknown'),
            "cache_mb": getattr(status, 'cache_mb', 0),
            "consecutive_failures": getattr(status, 'consecutive_failures', 0),
        }
        return context

    async def _enrich_context(self, workflow: Dict, device_ip: str, context: Dict) -> Dict:
        """Populate workflow/device metadata for template variables."""
        enriched = dict(context)
        enriched['workflow_name'] = workflow.get('name', 'Unknown')
        enriched['online'] = bool(enriched.get('online', False))
        scope = workflow.get('device_scope', {}) or {}
        scope_mode = scope.get('mode', 'all')
        enriched['scope_mode'] = scope_mode
        enriched['scope_plant_id'] = scope.get('plant_id', '') or ''
        if scope_mode == 'selected':
            selected_count = len(scope.get('ips', []) or [])
            enriched['scope_label'] = f"Selected ({selected_count} devices)"
        elif scope_mode == 'plant':
            plant_id = scope.get('plant_id', '') or '-'
            enriched['scope_label'] = f"Plant: {plant_id}"
        else:
            enriched['scope_label'] = "All devices"

        try:
            from adb_manager import adb_manager
            devices = await asyncio.to_thread(adb_manager.get_devices_from_csv)
            device = next((d for d in devices if d.get('IP') == device_ip), None)
        except Exception:
            device = None

        if device:
            enriched['device_name'] = device.get('Asset Name', '') or device_ip
            enriched['asset_name'] = device.get('Asset Name', '') or ''
            enriched['location'] = device.get('Default Location', '') or ''
            enriched['work_center'] = device.get('Work Center', '') or ''
            enriched['plant_id'] = device.get('Plant ID', '') or device.get('plant_id', '') or ''
            enriched['project'] = device.get('Project', '') or ''
            enriched['model'] = device.get('Model', '') or ''
            enriched['manufacturer'] = device.get('Manufacturer', '') or ''
            enriched['serial'] = device.get('Serial', '') or ''
        else:
            enriched.setdefault('device_name', device_ip)
            enriched.setdefault('asset_name', '')
            enriched.setdefault('location', '')
            enriched.setdefault('work_center', '')
            enriched.setdefault('plant_id', '')
            enriched.setdefault('project', '')
            enriched.setdefault('model', '')
            enriched.setdefault('manufacturer', '')
            enriched.setdefault('serial', '')

        return enriched

    async def _get_workflow_device_records(self, workflow: Dict) -> List[Dict]:
        """Get inventory records for a workflow based on device scope."""
        scope = workflow.get('device_scope', {}) or {}
        mode = scope.get('mode', 'all')

        try:
            from adb_manager import adb_manager
            devices = await asyncio.to_thread(adb_manager.get_devices_from_csv)
        except Exception:
            return []

        if mode == 'selected':
            selected_ips = set(scope.get('ips', []) or [])
            return [device for device in devices if device.get('IP') in selected_ips]

        if mode == 'plant':
            plant_id = (scope.get('plant_id') or '').strip()
            return [
                device for device in devices
                if (device.get('Plant ID') or device.get('plant_id') or '') == plant_id
            ]

        return devices

    # ========================================
    # Workflow Execution
    # ========================================

    async def _execute_workflow(self, workflow: Dict, device_ip: str,
                                trigger_type: str, trigger_context: Dict,
                                dry_run: bool = False) -> Dict:
        """Execute a workflow's node chain for a single device."""
        start_time = time.time()
        execution_started_at = self._now()
        workflow_id = workflow['id']
        workflow_name = workflow.get('name', 'Unknown')
        nodes = workflow.get('nodes', [])
        nodes_executed = []
        status = 'success'
        error_message = None

        mode_label = "dry-run" if dry_run else "live"
        logger.info(f"[AutomationEngine] Executing '{workflow_name}' for {device_ip} (trigger: {trigger_type}, mode: {mode_label})")

        # Inject workflow_id into context so actions can use it for dedup
        trigger_context['workflow_id'] = workflow_id
        trigger_context = await self._enrich_context(workflow, device_ip, trigger_context)

        try:
            # Find condition nodes
            condition_nodes = [n for n in nodes if n.get('category') == 'condition']
            action_nodes = [n for n in nodes if n.get('category') == 'action']

            # Evaluate conditions (all must pass)
            if condition_nodes:
                for cond in condition_nodes:
                    passed = await self._evaluate_condition(cond, device_ip, trigger_context)
                    nodes_executed.append({
                        "id": cond.get('id'),
                        "type": cond.get('type'),
                        "category": "condition",
                        "result": "passed" if passed else "failed"
                    })
                    if not passed:
                        status = 'skipped'
                        logger.info(f"[AutomationEngine] Condition '{cond.get('type')}' failed for {device_ip}, skipping actions")
                        break

            # Execute actions if conditions passed
            if status == 'success' and action_nodes:
                for action in action_nodes:
                    if dry_run:
                        nodes_executed.append({
                            "id": action.get('id'),
                            "type": action.get('type'),
                            "category": "action",
                            "result": "dry_run",
                            "message": f"Dry run: would execute {action.get('type')}"
                        })
                        continue
                    try:
                        result = await self._execute_action(action, device_ip, trigger_context)
                        nodes_executed.append({
                            "id": action.get('id'),
                            "type": action.get('type'),
                            "category": "action",
                            "result": "success" if result.get('success') else "failed",
                            "message": result.get('message', '')
                        })
                        if not result.get('success'):
                            status = 'partial'
                    except Exception as e:
                        nodes_executed.append({
                            "id": action.get('id'),
                            "type": action.get('type'),
                            "category": "action",
                            "result": "error",
                            "message": str(e)
                        })
                        status = 'failed'
                        error_message = str(e)

        except Exception as e:
            status = 'failed'
            error_message = str(e)
            logger.error(f"[AutomationEngine] Workflow execution error: {e}")

        duration_ms = int((time.time() - start_time) * 1000)
        execution_completed_at = self._now()

        if not dry_run:
            # Log execution
            from database import log_automation_execution, update_workflow_trigger_stats
            try:
                await asyncio.to_thread(
                    log_automation_execution,
                    workflow_id, workflow_name, device_ip, trigger_type,
                    trigger_context, nodes_executed, status, error_message, duration_ms,
                    execution_started_at, execution_completed_at
                )
                await asyncio.to_thread(update_workflow_trigger_stats, workflow_id, execution_completed_at)

                # Update cached last_triggered_at
                async with self._lock:
                    if workflow_id in self._workflows_cache:
                        self._workflows_cache[workflow_id]['last_triggered_at'] = self._now().isoformat()

            except Exception as e:
                logger.error(f"[AutomationEngine] Failed to log execution: {e}")

            # Set cooldown
            self._set_cooldown(workflow_id, device_ip)

            # Broadcast execution event via WebSocket
            try:
                await ws_manager.broadcast_json({
                    "type": "automation_execution",
                    "workflow_id": workflow_id,
                    "workflow_name": workflow_name,
                    "device_ip": device_ip,
                    "trigger_type": trigger_type,
                    "status": status,
                    "nodes_executed": nodes_executed,
                    "duration_ms": duration_ms,
                    "timestamp": self._now().isoformat()
                })
            except Exception:
                pass

        logger.info(f"[AutomationEngine] '{workflow_name}' for {device_ip}: {status} ({duration_ms}ms, mode: {mode_label})")
        return {
            "success": status in {"success", "skipped"},
            "status": status,
            "workflow_id": workflow_id,
            "workflow_name": workflow_name,
            "device_ip": device_ip,
            "trigger_type": trigger_type,
            "nodes_executed": nodes_executed,
            "error_message": error_message,
            "duration_ms": duration_ms,
            "dry_run": dry_run,
        }

    # ========================================
    # Condition Evaluators
    # ========================================

    async def _evaluate_condition(self, node: Dict, device_ip: str,
                                   context: Dict) -> bool:
        """Evaluate a condition node"""
        cond_type = node.get('type', '')
        config = node.get('config', {})

        if cond_type == 'consecutive_failures_gt':
            threshold = config.get('value', 3)
            return context.get('consecutive_failures', 0) > threshold

        elif cond_type == 'ram_lt':
            threshold = config.get('value', 300)
            # Need to run health check to get RAM
            try:
                from adb_manager import adb_manager
                result = await adb_manager.health_check(device_ip, include_cache=False)
                return result.get('ram_mb', 9999) < threshold
            except Exception:
                return False

        elif cond_type == 'cache_gt':
            threshold = config.get('value', 100)
            return context.get('cache_mb', 0) > threshold

        elif cond_type == 'storage_lt':
            threshold = config.get('value', 0.5)
            try:
                from adb_manager import adb_manager
                result = await adb_manager.health_check(device_ip, include_cache=False)
                return result.get('storage_free_gb', 999) < threshold
            except Exception:
                return False

        return False  # Unknown conditions fail safe

    # ========================================
    # Action Executors
    # ========================================

    async def _execute_action(self, node: Dict, device_ip: str,
                               context: Dict) -> Dict:
        """Execute a single action node"""
        action_type = node.get('type', '')
        config = node.get('config', {})
        package = config.get('package', DEFAULT_APP_PACKAGE)

        from adb_manager import adb_manager
        from alert_manager import alert_manager

        if action_type == 'restart_app':
            result = await adb_manager.open_app(device_ip, package)
            success = result.get('success', False) if isinstance(result, dict) else False
            return {"success": success, "message": result.get('message', str(result))}

        elif action_type == 'reboot_device':
            result = await adb_manager.reboot_device(device_ip)
            success = result.get('success', False) if isinstance(result, dict) else False
            return {"success": success, "message": result.get('message', str(result))}

        elif action_type == 'clear_cache':
            result = await adb_manager.clear_app_cache(device_ip, package)
            success = result.get('success', False) if isinstance(result, dict) else False
            return {"success": success, "message": result.get('message', str(result))}

        elif action_type == 'clear_app_data':
            result = await adb_manager.clear_app_data(device_ip, package)
            success = result.get('success', False) if isinstance(result, dict) else False
            return {"success": success, "message": result.get('message', str(result))}

        elif action_type == 'send_email':
            subject = config.get('subject', 'Automation Alert')
            # Replace variables in subject/body
            subject = self._replace_variables(subject, device_ip, context)
            body = self._build_alert_body(device_ip, context, config)
            html_body = self._build_alert_html(device_ip, context, config)
            success = await asyncio.to_thread(alert_manager.send_email, subject, body, html_body)
            return {"success": success, "message": "Email sent" if success else "Email failed"}

        elif action_type == 'send_syno_chat':
            message = config.get('message', 'Automation alert for {ip}')
            message = self._replace_variables(message, device_ip, context)
            success = await asyncio.to_thread(alert_manager.send_syno_chat, message)
            return {"success": success, "message": "Chat sent" if success else "Chat failed"}

        elif action_type == 'run_health_check':
            result = await adb_manager.health_check(device_ip)
            status_val = result.get('status', 'unknown') if isinstance(result, dict) else 'unknown'
            return {"success": status_val != 'error', "message": f"Health: {status_val}"}

        elif action_type == 'wake_device':
            result = await adb_manager.wake_device(device_ip)
            success = result.get('success', False) if isinstance(result, dict) else False
            return {"success": success, "message": result.get('message', 'Device woken')}

        elif action_type == 'sleep_device':
            result = await adb_manager.sleep_device(device_ip)
            success = result.get('success', False) if isinstance(result, dict) else False
            return {"success": success, "message": result.get('message', 'Device sleeping')}

        elif action_type == 'send_daily_report':
            try:
                from connection_checker import connection_checker
                all_status = await connection_checker.get_all_status()
                workflow_id = context.get('workflow_id')
                async with self._lock:
                    workflow = self._workflows_cache.get(workflow_id, {})
                scoped_devices = await self._get_workflow_device_records(workflow)

                devices_list = []
                for d in scoped_devices:
                    ip = d.get('IP', '')
                    st = all_status.get('devices', {}).get(ip, {})
                    devices_list.append({
                        'ip': ip,
                        'name': d.get('Asset Name', ''),
                        'location': d.get('Default Location', ''),
                        'workCenter': d.get('Work Center', ''),
                        'status': 'online' if st.get('online') else 'offline',
                        'appStatus': st.get('app_status', 'Unknown'),
                    })

                report_data = {
                    'total': len(devices_list),
                    'online': sum(1 for d in devices_list if d['status'] == 'online'),
                    'offline': sum(1 for d in devices_list if d['status'] != 'online'),
                    'devices': devices_list
                }

                template = None
                template_id = config.get('template_id', '')
                if template_id:
                    from database import get_template_by_id
                    template = await asyncio.to_thread(get_template_by_id, template_id)

                result = await asyncio.to_thread(
                    alert_manager.send_daily_report_result, report_data, template
                )
                if result.get("success"):
                    message = result.get("message", "Daily report sent")
                    recipients = result.get("recipients") or []
                    if recipients:
                        message = f"{message} (to: {', '.join(recipients)})"
                    return {"success": True, "message": message}
                return {
                    "success": False,
                    "message": result.get("message", "Report send failed"),
                }
            except Exception as e:
                return {"success": False, "message": f"Report error: {str(e)}"}

        return {"success": False, "message": f"Unknown action: {action_type}"}

    # ========================================
    # Helper Methods
    # ========================================

    async def _get_workflow_devices(self, workflow: Dict) -> List[str]:
        """Get list of device IPs for a workflow based on device_scope"""
        devices = await self._get_workflow_device_records(workflow)
        return [d.get('IP') for d in devices if d.get('IP')]

    def _check_cooldown(self, workflow_id: str, device_ip: str,
                        cooldown_minutes: int = 5) -> bool:
        """Check if cooldown has expired. Returns True if action is allowed."""
        key = f"{workflow_id}:{device_ip}"
        last = self._cooldowns.get(key)
        if last and (self._now() - last).total_seconds() < cooldown_minutes * 60:
            return False
        return True

    def _set_cooldown(self, workflow_id: str, device_ip: str):
        """Set cooldown timestamp"""
        key = f"{workflow_id}:{device_ip}"
        self._cooldowns[key] = self._now()

        # Clean old cooldowns periodically
        if len(self._cooldowns) > 1000:
            cutoff = self._now() - timedelta(hours=1)
            self._cooldowns = {k: v for k, v in self._cooldowns.items() if v > cutoff}

    def _replace_variables(self, text: str, device_ip: str, context: Dict) -> str:
        """Replace {ip}, {status}, {app_status} etc. in text"""
        now = self._now()
        replacements = {
            '{ip}': device_ip,
            '{device_name}': context.get('device_name', device_ip),
            '{asset_name}': context.get('asset_name', ''),
            '{location}': context.get('location', ''),
            '{work_center}': context.get('work_center', ''),
            '{plant_id}': context.get('plant_id', ''),
            '{project}': context.get('project', ''),
            '{model}': context.get('model', ''),
            '{manufacturer}': context.get('manufacturer', ''),
            '{serial}': context.get('serial', ''),
            '{workflow_name}': context.get('workflow_name', ''),
            '{online}': 'true' if context.get('online') else 'false',
            '{status}': 'Online' if context.get('online') else 'Offline',
            '{app_status}': context.get('app_status', 'unknown'),
            '{cache_mb}': str(round(context.get('cache_mb', 0), 1)),
            '{failures}': str(context.get('consecutive_failures', 0)),
            '{trigger}': context.get('trigger_type', 'unknown'),
            '{date}': now.strftime('%Y-%m-%d'),
            '{time}': now.strftime('%Y-%m-%d %H:%M:%S'),
            '{timestamp}': now.isoformat(timespec='seconds'),
        }
        for key, value in replacements.items():
            text = text.replace(key, value)
        return text

    def _build_alert_body(self, device_ip: str, context: Dict, config: Dict) -> str:
        """Build plain text alert body"""
        now = self._now()
        return f"""Automation Alert

Device: {device_ip}
Trigger: {context.get('trigger_type', 'unknown')}
App Status: {context.get('app_status', 'unknown')}
Device Online: {'Yes' if context.get('online') else 'No'}
Cache: {context.get('cache_mb', 0):.1f} MB
Time: {now.strftime('%Y-%m-%d %H:%M:%S')}

This is an automated alert from ADB Control Center.
"""

    def _build_alert_html(self, device_ip: str, context: Dict, config: Dict) -> str:
        """Build HTML alert body"""
        now = self._now()
        app_status = context.get('app_status', 'unknown')
        app_color = '#16a34a' if app_status == 'RUNNING' else '#dc2626' if app_status == 'STOPPED' else '#6b7280'
        online = context.get('online', False)
        online_color = '#16a34a' if online else '#dc2626'

        return f"""
<html>
<body style="font-family: Arial, sans-serif; padding: 20px;">
    <h2 style="color: #1f2937;">Automation Alert</h2>
    <table style="border-collapse: collapse; margin: 16px 0;">
        <tr>
            <td style="padding: 8px 16px; background: #f3f4f6; font-weight: bold;">Device</td>
            <td style="padding: 8px 16px; font-family: monospace;">{device_ip}</td>
        </tr>
        <tr>
            <td style="padding: 8px 16px; background: #f3f4f6; font-weight: bold;">Trigger</td>
            <td style="padding: 8px 16px;">{context.get('trigger_type', 'unknown')}</td>
        </tr>
        <tr>
            <td style="padding: 8px 16px; background: #f3f4f6; font-weight: bold;">App Status</td>
            <td style="padding: 8px 16px; color: {app_color}; font-weight: bold;">{app_status}</td>
        </tr>
        <tr>
            <td style="padding: 8px 16px; background: #f3f4f6; font-weight: bold;">Online</td>
            <td style="padding: 8px 16px; color: {online_color};">{'Yes' if online else 'No'}</td>
        </tr>
        <tr>
            <td style="padding: 8px 16px; background: #f3f4f6; font-weight: bold;">Cache</td>
            <td style="padding: 8px 16px;">{context.get('cache_mb', 0):.1f} MB</td>
        </tr>
        <tr>
            <td style="padding: 8px 16px; background: #f3f4f6; font-weight: bold;">Time</td>
            <td style="padding: 8px 16px;">{now.strftime('%Y-%m-%d %H:%M:%S')}</td>
        </tr>
    </table>
    <hr style="border: none; border-top: 1px solid #e5e7eb;">
    <p style="color: #9ca3af; font-size: 12px;">Automated by ADB Control Center</p>
</body>
</html>
"""

    def get_status(self) -> Dict:
        """Get engine status info"""
        return {
            "running": self._running,
            "workflow_count": len(self._workflows_cache),
            "active_cooldowns": len(self._cooldowns),
            "last_evaluation": self._last_evaluation.isoformat() if self._last_evaluation else None,
            "tracked_devices": len(self._prev_app_status),
        }


# Global instance
automation_engine = AutomationEngine()
