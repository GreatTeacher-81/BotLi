/**
 * BotLi Web Interface Client-Side Script
 * Handles Socket.IO communication and UI interactions.
 */
document.addEventListener('DOMContentLoaded', () => {
    // --- Constants and Elements ---
    const MAX_LOG_LINES = 500; // Limit log lines to prevent browser slowdown
    const socket = io(); // Connect to Socket.IO server (defaults to current host)

    const logOutput = document.getElementById('log-output');
    const connectionStatusBadge = document.querySelector('#connection-status > span.badge');
    const botStatusBadge = document.querySelector('#bot-status > span.badge');
    const matchmakingStatusBadge = document.querySelector('#matchmaking-status > span.badge');
    const usernameDisplay = document.getElementById('username-display');

    // Form Buttons/Elements (cache for performance)
    const challengeForm = document.getElementById('challenge-form');
    const challengeUsernameInput = document.getElementById('challenge-username');
    const rechallengeBtn = document.getElementById('rechallenge-btn');
    const createForm = document.getElementById('create-form');
    const createUsernameInput = document.getElementById('create-username');
    const startMatchmakingBtn = document.getElementById('start-matchmaking-btn');
    const stopMatchmakingBtn = document.getElementById('stop-matchmaking-btn');
    const resetMatchmakingForm = document.getElementById('reset-matchmaking-form');
    const tournamentForm = document.getElementById('tournament-form');
    const leaveTournamentBtn = document.getElementById('leave-tournament-btn');
    const blacklistForm = document.getElementById('blacklist-form');
    const blacklistUsernameInput = document.getElementById('blacklist-username');
    const whitelistForm = document.getElementById('whitelist-form');
    const whitelistUsernameInput = document.getElementById('whitelist-username');
    const clearQueueBtn = document.getElementById('clear-queue-btn');
    const quitBtn = document.getElementById('quit-btn');


    // --- Helper Functions ---

    /**
     * Logs a message to the web UI console.
     * @param {string} message The message content.
     * @param {string} [type='info'] The log type (e.g., 'info', 'success', 'error', 'game').
     */
    function logMessage(message, type = 'info') {
        if (!logOutput) return; // Exit if log area not found

        const timestamp = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        const logEntry = document.createElement('div');
        // Use specific classes for styling based on type
        logEntry.className = `log-${type}`;
        // Sanitize message slightly (replace < > to prevent HTML injection)
        const sanitizedMessage = message.replace(/</g, "<").replace(/>/g, ">");
        logEntry.innerHTML = `<span class="log-timestamp">${timestamp}</span> ${sanitizedMessage}`;

        logOutput.appendChild(logEntry);

        // Prune old log lines if exceeding max
        while (logOutput.children.length > MAX_LOG_LINES) {
            logOutput.removeChild(logOutput.firstChild);
        }

        // Auto-scroll to bottom only if user hasn't scrolled up
        // Basic check: is scroll near bottom?
        const isScrolledToBottom = logOutput.scrollHeight - logOutput.clientHeight <= logOutput.scrollTop + 30; // Allow small threshold
        if (isScrolledToBottom) {
            logOutput.scrollTop = logOutput.scrollHeight;
        }
    }

    /**
     * Updates the status badge in the navbar.
     * @param {HTMLElement} badgeElement The badge element to update.
     * @param {string} text The text content for the badge.
     * @param {string} bgClass The Bootstrap background class (e.g., 'bg-success', 'bg-danger').
     */
    function updateStatusBadge(badgeElement, text, bgClass) {
        if (!badgeElement) return;
        badgeElement.textContent = text;
        // Remove existing bg classes before adding new one
        badgeElement.className = 'badge'; // Reset classes
        badgeElement.classList.add(bgClass);
    }

    /**
     * Sends a command object to the server via Socket.IO.
     * @param {string} command The command name.
     * @param {object} [params={}] Optional parameters for the command.
     */
    function submitCommand(command, params = {}) {
        console.debug(`Emitting command: ${command}`, params); // Debug log in browser console
        socket.emit('submit_command', { command: command, params: params });
    }


    // --- Socket.IO Event Handlers ---

    socket.on('connect', () => {
        updateStatusBadge(connectionStatusBadge, 'Connected', 'bg-success');
        logMessage('Connected to BotLi server.', 'success');
        socket.emit('request_initial_status'); // Ask server for current bot state
    });

    socket.on('disconnect', (reason) => {
        updateStatusBadge(connectionStatusBadge, 'Disconnected', 'bg-danger');
        logMessage(`Disconnected from server: ${reason}`, 'error');
        // Reset other statuses on disconnect
        updateStatusBadge(botStatusBadge, 'Unknown', 'bg-secondary');
        updateStatusBadge(matchmakingStatusBadge, 'Unknown', 'bg-secondary');
        usernameDisplay.textContent = 'User: N/A';
        // Disable most controls on disconnect
        setControlsEnabled(false);
    });

    socket.on('connect_error', (err) => {
        updateStatusBadge(connectionStatusBadge, 'Conn. Error', 'bg-danger');
        logMessage(`Connection error: ${err.message}`, 'error');
        setControlsEnabled(false);
    });

    // Listen for log messages from the server
    socket.on('log_message', (data) => {
        logMessage(data.data, data.type || 'info');
    });

    // Listen for status updates from the server
    socket.on('status_update', (data) => {
        console.debug("Received status update:", data); // Debug
        if (data.bot_status) {
            let statusText = data.bot_status;
            let statusClass = 'bg-secondary'; // Default: Unknown/Stopped
            if (statusText === 'Running') {
                statusClass = 'bg-success';
                setControlsEnabled(true); // Enable controls when bot is running
            } else if (statusText.startsWith('Error')) {
                statusClass = 'bg-danger';
                setControlsEnabled(false); // Disable controls on error
            } else { // Stopped, Initializing, etc.
                statusClass = 'bg-warning';
                 setControlsEnabled(false); // Disable controls when not fully running
            }
            updateStatusBadge(botStatusBadge, statusText, statusClass);
        }
         if (data.username) {
             usernameDisplay.textContent = `User: ${data.username}`;
         }
        if (typeof data.matchmaking_status !== 'undefined') {
            const is_on = data.matchmaking_status;
            updateStatusBadge(matchmakingStatusBadge, is_on ? 'On' : 'Off', is_on ? 'bg-success' : 'bg-danger');
            // Enable/disable matchmaking buttons based on status
            if (startMatchmakingBtn) startMatchmakingBtn.disabled = is_on;
            if (stopMatchmakingBtn) stopMatchmakingBtn.disabled = !is_on;
        }
    });

    // --- UI Interaction Event Listeners ---

    /**
     * Enables or disables common control elements.
     * @param {boolean} isEnabled True to enable, false to disable.
     */
    function setControlsEnabled(isEnabled) {
        const controls = [
            challengeForm, rechallengeBtn, createForm, startMatchmakingBtn,
            stopMatchmakingBtn, resetMatchmakingForm, tournamentForm,
            leaveTournamentBtn, blacklistForm, whitelistForm, clearQueueBtn
        ];
        controls.forEach(control => {
            if (control) {
                 // Disable form elements within forms
                if (control.tagName === 'FORM') {
                    Array.from(control.elements).forEach(el => el.disabled = !isEnabled);
                } else { // Disable buttons directly
                    control.disabled = !isEnabled;
                }
            }
        });
         // Special handling for matchmaking buttons based on its specific status
        const matchmakingIsOn = matchmakingStatusBadge?.textContent === 'On';
        if(startMatchmakingBtn) startMatchmakingBtn.disabled = !isEnabled || matchmakingIsOn;
        if(stopMatchmakingBtn) stopMatchmakingBtn.disabled = !isEnabled || !matchmakingIsOn;
    }

    // Initial state: disable controls until confirmed running
    setControlsEnabled(false);

    // Challenge Form
    if (challengeForm) {
        challengeForm.addEventListener('submit', (e) => {
            e.preventDefault();
            const params = {
                username: document.getElementById('challenge-username')?.value,
                timecontrol: document.getElementById('challenge-timecontrol')?.value,
                color: document.getElementById('challenge-color')?.value,
                rated: document.getElementById('challenge-rated')?.value === 'true',
                variant: document.getElementById('challenge-variant')?.value
            };
            if (params.username && params.timecontrol) {
                submitCommand('challenge', params);
                // Optionally clear username after submitting
                // challengeUsernameInput.value = '';
            } else {
                logMessage('Username and Time Control are required for challenge.', 'warning');
            }
        });
    }

    // Rechallenge Button
    if (rechallengeBtn) {
        rechallengeBtn.addEventListener('click', () => {
            submitCommand('rechallenge');
        });
    }

    // Create Game Pair Form
    if (createForm) {
        createForm.addEventListener('submit', (e) => {
            e.preventDefault();
            const countInput = document.getElementById('create-count');
            const params = {
                count: parseInt(countInput?.value || '0', 10),
                username: document.getElementById('create-username')?.value,
                timecontrol: document.getElementById('create-timecontrol')?.value,
                rated: document.getElementById('create-rated')?.value === 'true',
                variant: document.getElementById('create-variant')?.value
            };
            if (params.username && params.count > 0 && params.timecontrol) {
                submitCommand('create', params);
                 // Optionally clear username after submitting
                // createUsernameInput.value = '';
            } else {
                logMessage('Username, valid Time Control, and a positive Count are required to create pairs.', 'warning');
            }
        });
    }

    // Matchmaking Buttons
    if (startMatchmakingBtn) {
        startMatchmakingBtn.addEventListener('click', () => {
            submitCommand('matchmaking');
        });
    }
    if (stopMatchmakingBtn) {
        stopMatchmakingBtn.addEventListener('click', () => {
            submitCommand('stop');
        });
    }

    // Reset Matchmaking Form
    if (resetMatchmakingForm) {
        resetMatchmakingForm.addEventListener('submit', (e) => {
            e.preventDefault();
            const params = {
                perf_type: document.getElementById('reset-perf-type')?.value
            };
            if (params.perf_type) {
                submitCommand('reset', params);
            } else {
                logMessage('Please select a Performance Type to reset matchmaking.', 'warning');
            }
        });
    }

    // Tournament Form
    if (tournamentForm) {
        tournamentForm.addEventListener('submit', (e) => {
            e.preventDefault();
            const tournamentIdInput = document.getElementById('tournament-id');
            const params = {
                tournament_id: tournamentIdInput?.value,
                team: document.getElementById('tournament-team')?.value || null, // Send null if empty
                password: document.getElementById('tournament-password')?.value || null // Send null if empty
            };
            if (params.tournament_id) {
                submitCommand('tournament', params);
                // Optionally clear fields after submit
                 // tournamentIdInput.value = '';
                 // document.getElementById('tournament-team').value = '';
                 // document.getElementById('tournament-password').value = '';
            } else {
                logMessage('Tournament ID is required to join.', 'warning');
            }
        });
    }

    // Leave Tournament Button
    if (leaveTournamentBtn) {
        leaveTournamentBtn.addEventListener('click', () => {
            // Use the ID currently entered in the tournament join form
            const tournamentId = document.getElementById('tournament-id')?.value;
            if (tournamentId) {
                submitCommand('leave', { tournament_id: tournamentId });
            } else {
                logMessage('Enter the Tournament ID in the field above to leave it.', 'warning');
            }
        });
    }

    // Blacklist Form
    if (blacklistForm) {
        blacklistForm.addEventListener('submit', (e) => {
            e.preventDefault();
            const username = blacklistUsernameInput?.value;
            if (username) {
                submitCommand('blacklist', { username: username });
                blacklistUsernameInput.value = ''; // Clear field after submit
            } else {
                logMessage('Username is required for blacklist.', 'warning');
            }
        });
    }

    // Whitelist Form
    if (whitelistForm) {
        whitelistForm.addEventListener('submit', (e) => {
            e.preventDefault();
            const username = whitelistUsernameInput?.value;
            if (username) {
                submitCommand('whitelist', { username: username });
                whitelistUsernameInput.value = ''; // Clear field after submit
            } else {
                logMessage('Username is required for whitelist.', 'warning');
            }
        });
    }

    // Clear Challenge Queue Button
    if (clearQueueBtn) {
        clearQueueBtn.addEventListener('click', () => {
            submitCommand('clear');
        });
    }

    // Quit Button
    if (quitBtn) {
        quitBtn.addEventListener('click', () => {
            // Add confirmation dialog
            if (confirm('Are you sure you want to stop the bot and shut down the web server?')) {
                submitCommand('quit');
                // Disable button after clicking to prevent multiple clicks
                quitBtn.disabled = true;
                quitBtn.textContent = 'Shutting Down...';
                updateStatusBadge(connectionStatusBadge, 'Disconnecting', 'bg-warning');
            }
        });
    }

}); // End DOMContentLoaded
