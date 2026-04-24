/**
 * ADB Control Center Web App - Frontend JavaScript
 * Handles WebSocket connection, device management, and UI interactions
 */

// Global state
let devices = [];
let selectedIps = new Set();
let ws = null;
let wsReconnectAttempts = 0;
const MAX_RECONNECT_ATTEMPTS = 5;

// ============================================
// WebSocket Management
// ============================================

function connectWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws`;

    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
        console.log('WebSocket connected');
        wsReconnectAttempts = 0;
        addLog('WebSocket connected', 'success');
    };

    ws.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            handleWebSocketMessage(data);
        } catch (e) {
            console.error('WebSocket message parse error:', e);
        }
    };

    ws.onclose = () => {
        console.log('WebSocket disconnected');
        if (wsReconnectAttempts < MAX_RECONNECT_ATTEMPTS) {
            wsReconnectAttempts++;
            setTimeout(connectWebSocket, 2000 * wsReconnectAttempts);
        }
    };

    ws.onerror = (error) => {
        console.error('WebSocket error:', error);
    };
}

function handleWebSocketMessage(data) {
    switch (data.type) {
        case 'log':
            addLog(data.message, data.level);
            break;
        case 'progress':
            updateProgress(data.current, data.total, data.message);
            break;
        case 'complete':
            hideProgress();
            if (data.success) {
                showToast(data.message, 'success');
            } else {
                showToast(data.message, 'error');
            }
            break;
        case 'status':
            updateDeviceStatus(data.ip, data.status, data.details);
            break;
    }
}

// ============================================
// Log Panel
// ============================================

function addLog(message, level = 'info') {
    const logPanel = document.getElementById('logPanel');
    const timestamp = new Date().toLocaleTimeString('th-TH');

    const entry = document.createElement('div');
    entry.className = 'log-entry';

    let levelClass = '';
    switch (level) {
        case 'success': levelClass = 'log-success'; break;
        case 'error': levelClass = 'log-error'; break;
        case 'warning': levelClass = 'log-warning'; break;
        default: levelClass = 'log-info';
    }

    entry.innerHTML = `<span class="log-time">[${timestamp}]</span><span class="${levelClass}">${escapeHtml(message)}</span>`;
    logPanel.appendChild(entry);
    logPanel.scrollTop = logPanel.scrollHeight;
}

function clearLog() {
    document.getElementById('logPanel').innerHTML = '';
    addLog('Log cleared', 'info');
}

// ============================================
// Device List Management
// ============================================

async function loadDevices(search = '') {
    try {
        const url = search ? `/api/devices?search=${encodeURIComponent(search)}` : '/api/devices';
        console.log('[Search] fetching:', url);
        const response = await fetch(url);
        const data = await response.json();
        devices = data.devices || [];
        console.log('[Search] results:', devices.length);
        renderDeviceTable();
        updateDeviceCount();
    } catch (error) {
        console.error('Error loading devices:', error);
        showToast('Failed to load devices', 'error');
    }
}

function renderDeviceTable() {
    const tbody = document.getElementById('deviceTableBody');
    tbody.innerHTML = '';

    devices.forEach((device, index) => {
        const ip = device.IP || '';
        const isSelected = selectedIps.has(ip);

        const tr = document.createElement('tr');
        tr.className = isSelected ? 'selected' : '';
        tr.dataset.ip = ip;

        tr.innerHTML = `
            <td class="px-3 py-2">
                <input type="checkbox" class="device-checkbox" ${isSelected ? 'checked' : ''}
                    onchange="toggleDevice('${ip}', this.checked)">
            </td>
            <td class="px-3 py-2 font-mono text-sm">${escapeHtml(ip)}</td>
            <td class="px-3 py-2">${escapeHtml(device['Asset Name'] || '')}</td>
            <td class="px-3 py-2">${escapeHtml(device['Default Location'] || '')}</td>
            <td class="px-3 py-2">${escapeHtml(device['Work Center'] || '')}</td>
            <td class="px-3 py-2">${escapeHtml(device['Model'] || '')}</td>
            <td class="px-3 py-2">${escapeHtml(device['Serial'] || '')}</td>
            <td class="px-3 py-2 text-center">
                <button onclick="launchScrcpy('${ip}'); event.stopPropagation();"
                        class="px-2 py-1 bg-green-600 hover:bg-green-700 text-white text-xs rounded mr-1"
                        title="Real-time 60 FPS">
                    Scrcpy
                </button>
                <button onclick="openRemote('${ip}'); event.stopPropagation();"
                        class="px-2 py-1 bg-indigo-600 hover:bg-indigo-700 text-white text-xs rounded"
                        title="Web-based remote">
                    Remote
                </button>
            </td>
        `;

        tr.addEventListener('click', (e) => {
            if (e.target.type !== 'checkbox') {
                toggleDevice(ip, !isSelected);
            }
        });

        tbody.appendChild(tr);
    });
}

function toggleDevice(ip, selected) {
    if (selected) {
        selectedIps.add(ip);
    } else {
        selectedIps.delete(ip);
    }
    renderDeviceTable();
    updateSelectionCount();
}

function selectAll() {
    devices.forEach(d => selectedIps.add(d.IP));
    renderDeviceTable();
    updateSelectionCount();
}

function deselectAll() {
    selectedIps.clear();
    renderDeviceTable();
    updateSelectionCount();
}

function updateDeviceCount() {
    document.getElementById('totalDevices').textContent = devices.length;
}

function updateSelectionCount() {
    document.getElementById('selectedCount').textContent = selectedIps.size;
}

function getSelectedIps() {
    return Array.from(selectedIps);
}

function getSelectedDevices() {
    return devices.filter(d => selectedIps.has(d.IP));
}

// Open Remote Control in new tab
function openRemote(ip) {
    window.open(`/remote/${ip}`, '_blank');
}

// ============================================
// Client Agent Detection (native scrcpy on client PCs)
// ============================================
let clientAgentAvailable = false;
let clientAgentUrl = localStorage.getItem('scrcpyAgentUrl') || `http://${window.location.hostname}:18080`;

async function checkClientAgent() {
    try {
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 3000);
        const response = await fetch(`${clientAgentUrl}/api/status`, {
            signal: controller.signal
        });
        clearTimeout(timeout);
        if (response.ok) {
            const data = await response.json();
            clientAgentAvailable = data.scrcpy_exists && data.adb_exists;
        } else {
            clientAgentAvailable = false;
        }
    } catch (e) {
        clientAgentAvailable = false;
    }
}

// Check on load
checkClientAgent();

// Launch native scrcpy window via Client Agent
async function launchScrcpy(ip) {
    // Re-read agent URL in case user changed it on Scrcpy Agent page
    clientAgentUrl = localStorage.getItem('scrcpyAgentUrl') || `http://${window.location.hostname}:18080`;

    if (!clientAgentAvailable) {
        // Re-check once before giving up
        await checkClientAgent();
        if (!clientAgentAvailable) {
            addLog(`[${ip}] Scrcpy Agent is not connected`, 'error');
            showToast('Scrcpy Agent is not connected.\nSet up the agent on the Scrcpy Agent page first.', 'error');
            return;
        }
    }

    addLog(`Launching scrcpy for ${ip} via agent...`);
    try {
        const response = await fetch(`${clientAgentUrl}/api/launch/${ip}`, { method: 'POST' });
        const data = await response.json();
        if (data.success) {
            addLog(`[${ip}] Scrcpy window opened - check agent PC desktop!`, 'success');
            showToast('Scrcpy launched! Check agent PC desktop.', 'success');
        } else {
            addLog(`[${ip}] Failed: ${data.message}`, 'error');
            showToast(`Failed to launch scrcpy: ${data.message}`, 'error');
        }
    } catch (e) {
        console.error('Launch scrcpy error:', e);
        addLog(`[${ip}] Scrcpy Agent unreachable`, 'error');
        showToast('Scrcpy Agent is unreachable.', 'error');
    }
}

// ============================================
// Progress Bar
// ============================================

function showProgress() {
    document.getElementById('progressContainer').classList.remove('hidden');
}

function hideProgress() {
    document.getElementById('progressContainer').classList.add('hidden');
}

function updateProgress(current, total, message = '') {
    showProgress();
    const percent = Math.round((current / total) * 100);
    document.getElementById('progressBar').style.width = `${percent}%`;
    document.getElementById('progressText').textContent = `${current}/${total} ${message}`;
}

// ============================================
// Toast Notifications
// ============================================

function showToast(message, type = 'info') {
    const container = document.getElementById('toastContainer');
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.textContent = message;
    container.appendChild(toast);

    setTimeout(() => {
        toast.remove();
    }, 3000);
}

// ============================================
// API Actions
// ============================================

async function apiCall(endpoint, method = 'POST', body = null) {
    const ips = getSelectedIps();
    if (ips.length === 0) {
        showToast('Please select at least one device', 'error');
        return null;
    }

    try {
        const options = {
            method,
            headers: { 'Content-Type': 'application/json' }
        };

        if (body) {
            // Merge ips with the provided body
            options.body = JSON.stringify({ ips, ...body });
        } else if (method === 'POST') {
            // Wrap ips in object format expected by backend
            options.body = JSON.stringify({ ips });
        }

        const response = await fetch(endpoint, options);
        return await response.json();
    } catch (error) {
        console.error('API call error:', error);
        showToast('API call failed', 'error');
        return null;
    }
}

// Connection actions
async function pingDevices() {
    await apiCall('/api/devices/ping');
}

async function connectDevices() {
    await apiCall('/api/devices/connect');
}

async function refreshConnected() {
    try {
        const response = await fetch('/api/devices/connected');
        const data = await response.json();
        addLog(`Online devices: ${data.total}`, 'info');
        if (data.connected.length > 0) {
            addLog(data.connected.join(', '), 'info');
        }
    } catch (error) {
        showToast('Failed to refresh', 'error');
    }
}

// Device actions
async function deviceAction(mode) {
    const ips = getSelectedIps();
    if (ips.length === 0) {
        showToast('Please select at least one device', 'error');
        return;
    }

    try {
        const response = await fetch('/api/devices/action', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ips: ips, mode: mode })
        });
        await response.json();
    } catch (error) {
        showToast('Action failed', 'error');
    }
}

async function healthCheck() {
    await apiCall('/api/devices/health');
}

async function renameDevices() {
    const newName = prompt('Enter new device name:');
    if (!newName) return;

    const ips = getSelectedIps();
    if (ips.length === 0) {
        showToast('Please select at least one device', 'error');
        return;
    }

    try {
        const response = await fetch('/api/devices/rename', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ips, new_name: newName })
        });
        await response.json();
    } catch (error) {
        showToast('Rename failed', 'error');
    }
}

async function rebootDevices() {
    if (!confirm('Are you sure you want to reboot selected devices?')) return;
    await apiCall('/api/devices/reboot');
}

async function shutdownDevices() {
    if (!confirm('Are you sure you want to shutdown selected devices?')) return;
    await apiCall('/api/devices/shutdown');
}

// App actions
async function openApp() {
    await apiCall('/api/app/open');
}

async function checkAppStatus() {
    await apiCall('/api/app/status');
}

async function clearAppData() {
    if (!confirm('Are you sure you want to clear app data?')) return;
    await apiCall('/api/app/clear');
}

async function installApk() {
    const fileInput = document.getElementById('apkFileInput');
    fileInput.click();
}

async function handleApkUpload(input) {
    if (!input.files || input.files.length === 0) return;

    const ips = getSelectedIps();
    if (ips.length === 0) {
        showToast('Please select at least one device', 'error');
        return;
    }

    const formData = new FormData();
    formData.append('file', input.files[0]);
    ips.forEach(ip => formData.append('ips', ip));

    try {
        const response = await fetch('/api/app/install', {
            method: 'POST',
            body: formData
        });
        await response.json();
    } catch (error) {
        showToast('Install failed', 'error');
    }

    input.value = '';
}

// Report
async function generateReport() {
    const selectedDevices = getSelectedDevices();
    if (selectedDevices.length === 0) {
        showToast('Please select at least one device', 'error');
        return;
    }

    try {
        const response = await fetch('/api/report/generate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(selectedDevices)
        });
        const data = await response.json();

        if (data.success) {
            // Download the report
            window.open(`/api/report/download/${data.filename}`, '_blank');
        }
    } catch (error) {
        showToast('Report generation failed', 'error');
    }
}

// ============================================
// Utility Functions
// ============================================

function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function debounce(func, wait) {
    let timeout;
    return function executedFunction(...args) {
        const later = () => {
            clearTimeout(timeout);
            func(...args);
        };
        clearTimeout(timeout);
        timeout = setTimeout(later, wait);
    };
}

// ============================================
// Event Listeners
// ============================================

document.addEventListener('DOMContentLoaded', () => {
    // Load user info into sidebar
    loadUserInfo();

    // Connect WebSocket
    connectWebSocket();

    // Load devices
    loadDevices();

    // Search input with debounce
    const searchInput = document.getElementById('searchInput');
    if (searchInput) {
        searchInput.addEventListener('input', debounce((e) => {
            console.log('[Search] input:', e.target.value);
            loadDevices(e.target.value);
        }, 300));
    }

    // APK file input handler
    document.getElementById('apkFileInput').addEventListener('change', function() {
        handleApkUpload(this);
    });

    addLog('ADB Control Center Web App initialized', 'success');
});

// Handle page visibility for WebSocket reconnection
document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && (!ws || ws.readyState !== WebSocket.OPEN)) {
        connectWebSocket();
    }
});

// Load user info into sidebar
async function loadUserInfo() {
    try {
        const response = await fetch('/api/auth/check');
        const data = await response.json();
        if (data.authenticated) {
            const u = data.user;
            const el = (id) => document.getElementById(id);
            if (el('userName')) el('userName').textContent = u.username;
            if (el('userRole')) el('userRole').textContent = u.role === 'admin' ? 'Administrator' : 'User';
            if (el('userInitial')) el('userInitial').textContent = u.username[0].toUpperCase();
        } else {
            window.location.href = '/login';
        }
    } catch (e) {
        console.error('Load user info error:', e);
    }
}

// Logout function
async function logout() {
    try {
        await fetch('/api/auth/logout', { method: 'POST' });
    } catch (e) {
        console.error('Logout error:', e);
    }
    window.location.href = '/login';
}
