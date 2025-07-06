// Auto-generation and UI management for summary page
let isGenerating = false;
let statusCheckInterval = null;

// Create status container if it doesn't exist
function createStatusContainer() {
    let statusContainer = document.getElementById('status-container');
    if (!statusContainer) {
        statusContainer = document.createElement('div');
        statusContainer.id = 'status-container';
        statusContainer.style.cssText = `
            position: fixed;
            top: 20px;
            right: 20px;
            z-index: 1000;
            max-width: 300px;
        `;
        document.body.appendChild(statusContainer);
    }
    return statusContainer;
}

// Initialize auto-generation if needed
function initializeAutoGeneration() {
    if (isGenerating) return;
    
    console.log('Starting auto-generation of action items...');
    createStatusContainer();
    generatePendingActions();
    startStatusPolling();
}

// Generate pending action items
async function generatePendingActions() {
    if (isGenerating) return;
    
    isGenerating = true;
    updateStatusMessage('Generating action items...', 'status');
    
    try {
        const response = await fetch('/generate_pending_actions', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            }
        });
        
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        
        const data = await response.json();
        
        if (data.success) {
            updateStatusMessage(data.message, 'success');
            // Refresh the page content after a short delay
            setTimeout(() => {
                window.location.reload();
            }, 2000);
        } else {
            updateStatusMessage(data.message || 'Error generating action items', 'error');
        }
    } catch (error) {
        console.error('Error generating actions:', error);
        updateStatusMessage('Error generating action items', 'error');
    } finally {
        isGenerating = false;
        stopStatusPolling();
    }
}

// Start polling for status updates
function startStatusPolling() {
    if (statusCheckInterval) return;
    
    statusCheckInterval = setInterval(async () => {
        try {
            const response = await fetch('/get_summary_status');
            
            if (!response.ok) {
                console.error('Status check failed:', response.status);
                return;
            }
            
            const data = await response.json();
            
            if (data.pending_count === 0) {
                // All items processed, refresh the page
                updateStatusMessage('All action items processed!', 'success');
                setTimeout(() => {
                    window.location.reload();
                }, 1000);
            } else {
                updateStatusMessage(`Processing ${data.pending_count} emails...`, 'status');
            }
        } catch (error) {
            console.error('Error checking status:', error);
        }
    }, 3000); // Check every 3 seconds
}

// Stop status polling
function stopStatusPolling() {
    if (statusCheckInterval) {
        clearInterval(statusCheckInterval);
        statusCheckInterval = null;
    }
}

// Update status message
function updateStatusMessage(message, type = 'status') {
    const statusContainer = createStatusContainer();
    const existingMessage = document.getElementById('status-message');
    
    if (existingMessage) {
        existingMessage.remove();
    }
    
    const messageDiv = document.createElement('div');
    messageDiv.id = 'status-message';
    messageDiv.textContent = message;
    
    // Add styling based on type
    let baseStyle = `
        padding: 10px 15px;
        border-radius: 5px;
        margin: 5px 0;
        font-size: 14px;
        font-weight: 500;
        box-shadow: 0 2px 8px rgba(0,0,0,0.1);
    `;
    
    switch (type) {
        case 'success':
            messageDiv.style.cssText = baseStyle + `
                background-color: #d4edda;
                border: 1px solid #c3e6cb;
                color: #155724;
            `;
            break;
        case 'error':
            messageDiv.style.cssText = baseStyle + `
                background-color: #f8d7da;
                border: 1px solid #f5c6cb;
                color: #721c24;
            `;
            break;
        default:
            messageDiv.style.cssText = baseStyle + `
                background-color: #d1ecf1;
                border: 1px solid #bee5eb;
                color: #0c5460;
            `;
            if (type === 'status') {
                messageDiv.innerHTML = message + ' <span style="animation: spin 1s linear infinite; display: inline-block;">⟳</span>';
            }
    }
    
    statusContainer.appendChild(messageDiv);
    
    // Auto-remove success and error messages after 5 seconds
    if (type === 'success' || type === 'error') {
        setTimeout(() => {
            if (messageDiv.parentNode) {
                messageDiv.remove();
            }
        }, 5000);
    }
}

// Add CSS for spinning animation
const style = document.createElement('style');
style.textContent = `
    @keyframes spin {
        from { transform: rotate(0deg); }
        to { transform: rotate(360deg); }
    }
`;
document.head.appendChild(style);

// Expand/Collapse functionality
document.addEventListener('DOMContentLoaded', function() {
    const expands = document.querySelectorAll('.expand');
    const emails = document.querySelectorAll('.email');

    for (let i = 0; i < expands.length; i++){
        expands[i].addEventListener("click", () => {
            if (emails[i].style.display === "none" || emails[i].style.display === "") {
                emails[i].style.display = "flex";
                expands[i].textContent = "Collapse";
            } else {
                emails[i].style.display = "none";
                expands[i].textContent = "Expand";
            }
        });
    }
});

// Remove items functionality
document.addEventListener('DOMContentLoaded', function() {
    const removes = document.querySelectorAll(".remove");

    for (let i = 0; i < removes.length; i++){
        removes[i].addEventListener("click", async function() {
            const todoId = this.dataset.todoId;
            
            if (!todoId) {
                updateStatusMessage('Error: No todo ID found', 'error');
                return;
            }
            
            try {
                const response = await fetch("/remove_todo", {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/json"
                    },
                    body: JSON.stringify({id: todoId})
                });
                
                if (!response.ok) {
                    throw new Error(`HTTP error! status: ${response.status}`);
                }
                
                const data = await response.json();
                
                if (data.success) {
                    // Remove the item from the DOM
                    const itemwrap = this.closest('.itemwrap');
                    const emailDiv = document.querySelector(`.email[data-id="${i}"]`);
                    
                    if (itemwrap) {
                        itemwrap.remove();
                    }
                    if (emailDiv) {
                        emailDiv.remove();
                    }
                    
                    updateStatusMessage('Item removed successfully', 'success');
                } else {
                    updateStatusMessage(data.message || 'Error removing item', 'error');
                }
            } catch (error) {
                console.error('Error removing todo:', error);
                updateStatusMessage('Error removing item', 'error');
            }
        });
    }
});

// Reply functionality
document.addEventListener('DOMContentLoaded', function() {
    const reply_btns = document.querySelectorAll('.reply');
    const reply_divs = document.querySelectorAll('.replydiv');

    for (let i = 0; i < reply_btns.length; i++){
        reply_btns[i].addEventListener('click', () => {
            if (reply_btns[i].textContent === "Reply"){
                reply_divs[i].classList.add('replycontain');
                reply_divs[i].classList.remove('replywrap');
                reply_btns[i].textContent = "Hide Reply";
            } else {
                reply_divs[i].classList.remove('replycontain');
                reply_divs[i].classList.add('replywrap');
                reply_btns[i].textContent = "Reply";
            }
        });
    }
});

// Reply submission functionality
document.addEventListener('DOMContentLoaded', function() {
    const reply_smts = document.querySelectorAll('.replysubmit');
    const bodys = document.querySelectorAll(".body");
    const ccs = document.querySelectorAll(".cc");
    const bccs = document.querySelectorAll('.bcc');

    for (let i = 0; i < reply_smts.length; i++){
        reply_smts[i].addEventListener('click', async (e) => {
            e.preventDefault();
            
            const body = bodys[i].value;
            const cc = ccs[i].value;
            const bcc = bccs[i].value;
            const subject = reply_smts[i].dataset.subject;
            const from = reply_smts[i].dataset.from;
            
            if (!body.trim()) {
                updateStatusMessage('Please enter a reply message', 'error');
                return;
            }
            
            try {
                const response = await fetch("/reply", {
                    method: "POST", 
                    headers:{
                        "Content-Type": "application/json"
                    },
                    body: JSON.stringify({
                        from: from, 
                        subject: subject,
                        cc: cc, 
                        bcc: bcc, 
                        body: body
                    })
                });
                
                if (!response.ok) {
                    throw new Error(`HTTP error! status: ${response.status}`);
                }
                
                const data = await response.json();
                
                if (data.success === true) {
                    bodys[i].value = "";
                    ccs[i].value = "";
                    bccs[i].value = "";
                    updateStatusMessage('Reply sent successfully', 'success');
                } else {
                    updateStatusMessage('Error sending reply', 'error');
                }
            } catch (error) {
                console.error('Error sending reply:', error);
                updateStatusMessage('Error sending reply', 'error');
            }
        });
    }
});

// Calendar forms functionality
function setupStaticCalendarForms() {
    const calendarBtns = document.querySelectorAll('.calendarbtn');
    
    calendarBtns.forEach(btn => {
        const calendar_form = btn.nextElementSibling;
        
        if (calendar_form && calendar_form.classList.contains('calendarform')) {
            btn.addEventListener("click", function() {
                if (calendar_form.style.display === "none" || calendar_form.style.display === "") {
                    calendar_form.style.display = "flex";
                } else {
                    calendar_form.style.display = "none";
                }
            });
            
            const submitBtn = calendar_form.querySelector('button[type="button"]');
            
            if (submitBtn) {
                submitBtn.addEventListener("click", async function(e) {
                    e.preventDefault();
                    
                    const nameInput = calendar_form.querySelector('input[name="name"]');
                    const startInput = calendar_form.querySelector('input[name="start"]');
                    const endInput = calendar_form.querySelector('input[name="end"]');
                    
                    if (!nameInput.value.trim() || !startInput.value || !endInput.value) {
                        updateStatusMessage('Please fill in all calendar fields', 'error');
                        return;
                    }
                    
                    const formData = new FormData();
                    formData.append('name', nameInput.value);
                    formData.append('start', startInput.value);
                    formData.append('end', endInput.value);
                    
                    try {
                        const response = await fetch('/add_to_calendar', {
                            method: 'POST',
                            body: formData
                        });
                        
                        if (!response.ok) {
                            throw new Error(`HTTP error! status: ${response.status}`);
                        }
                        
                        const result = await response.json();
                        
                        if (result.success === true) {
                            updateStatusMessage('Event added to calendar successfully!', 'success');
                            calendar_form.style.display = "none";
                            nameInput.value = '';
                            startInput.value = '';
                            endInput.value = '';
                        } else {
                            updateStatusMessage('Failed to add event to calendar', 'error');
                        }
                    } catch (error) {
                        console.error('Error adding event to calendar:', error);
                        updateStatusMessage('Error adding event to calendar', 'error');
                    }
                });
            }
        }
    });
}

// Initialize calendar forms when page loads
document.addEventListener('DOMContentLoaded', setupStaticCalendarForms);

// Cleanup on page unload
window.addEventListener('beforeunload', function() {
    stopStatusPolling();
});