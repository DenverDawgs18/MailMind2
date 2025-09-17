// Sequential processing for summary page - mimics emails() behavior
let isProcessing = false;

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
        transition: opacity 0.3s ease;
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
                messageDiv.style.opacity = '0';
                setTimeout(() => messageDiv.remove(), 300);
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

// Update header with current counts
function updateHeaderCounts(processed, total) {
    const header = document.querySelector('.summaryheader');
    if (header && total > 0) {
        const remaining = total - processed;
        if (remaining > 0) {
            header.textContent = `All generated action items of the last 24 hours of emails (Processing ${remaining} emails for action items...):`;
        } else {
            const todoCount = document.querySelectorAll('.itemwrap[data-todo-id]:not([data-todo-id=""])').length;
            header.textContent = `All generated action items of the last 24 hours of emails (Found ${todoCount} action items):`;
        }
    }
}

// Sequential processing function - similar to processEmailsSequentially in emails.js
async function processEmailsSequentially(emails) {
    if (isProcessing) return;
    
    isProcessing = true;
    let processed = 0;
    const total = emails.length;
    
    updateStatusMessage(`Processing ${total} emails for action items...`, 'status');
    
    for (let j = 0; j < emails.length; j++) {
        const email = emails[j];
        const itemwrap = email.closest('.itemwrap');
        const todoElement = itemwrap.querySelector('.item');
        let body = null;

        // Find the corresponding email body
        const emailDiv = document.querySelector(`.email[data-id="${j}"]`);
        if (emailDiv) {
            const bodyElement = emailDiv.querySelector('.etext');
            if (bodyElement) {
                body = bodyElement.textContent || bodyElement.innerText;
            }
        }

        if (todoElement && todoElement.textContent.includes("Generating ...") && body) {
            try {
                const response = await fetch("/get_summary_action", {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/json"
                    },
                    body: JSON.stringify({ body: body, index: j })
                }); 

                const data = await response.json();
                
                if (data.success) {
                    // Update the todo text
                    todoElement.textContent = `• ${data.action_item}`;
                    
                    // Update the itemwrap with todo ID if it's a real todo
                    if (data.todo_id) {
                        itemwrap.dataset.todoId = data.todo_id;
                        
                        // Update the remove button
                        const removeBtn = itemwrap.querySelector('.remove');
                        if (removeBtn) {
                            removeBtn.dataset.todoId = data.todo_id;
                        }
                    } else {
                        // No todo ID means "No action" - hide this item
                        itemwrap.style.transition = 'opacity 0.3s ease';
                        itemwrap.style.opacity = '0';
                        setTimeout(() => {
                            itemwrap.remove();
                            if (emailDiv) emailDiv.remove();
                        }, 300);
                    }
                    
                    // Add calendar button if it's a calendar event
                    if (data.calendar && data.todo_id) {
                        const existingCalendarBtn = itemwrap.querySelector('.calendarbtn');
                        if (!existingCalendarBtn) {
                            const calendarBtn = document.createElement('button');
                            calendarBtn.className = 'calendarbtn';
                            calendarBtn.textContent = 'Add to Calendar';
                            
                            // Create calendar form
                            const calendarForm = document.createElement('form');
                            calendarForm.className = 'calendarform';
                            calendarForm.style.display = 'none';
                            calendarForm.innerHTML = `
                                <label for="name-${j}">Name:</label>
                                <input type="text" name="name" id="name-${j}">
                                <label for="start-${j}">Start:</label>
                                <input type="datetime-local" id="start-${j}" name="start">
                                <label for="end-${j}">End:</label>
                                <input type="datetime-local" name="end" id="end-${j}">
                                <button type="button">Submit</button>
                            `;
                            
                            // Insert before the last element in itemwrap
                            itemwrap.appendChild(calendarBtn);
                            itemwrap.appendChild(calendarForm);
                            
                            // Set up calendar form functionality
                            setupCalendarForm(calendarBtn, calendarForm);
                        }
                    }
                    
                    processed++;
                    updateHeaderCounts(processed, total);
                    
                } else {
                    console.error("Error in response:", data.error);
                    todoElement.textContent = "• Error generating action item";
                }
            } catch (err) {
                console.error("Error fetching todo action item:", err);
                todoElement.textContent = "• Error generating action item";
            }
        }
        
        // Small delay between requests to avoid overwhelming the server
        await new Promise(resolve => setTimeout(resolve, 100));
    }
    
    isProcessing = false;
    
    // Final status update
    const finalTodoCount = document.querySelectorAll('.itemwrap[data-todo-id]:not([data-todo-id=""])').length;
    if (finalTodoCount > 0) {
        updateStatusMessage(`Generated ${finalTodoCount} action items successfully!`, 'success');
    } else {
        updateStatusMessage('No action items found in recent emails.', 'success');
    }
}

// Setup calendar form functionality
function setupCalendarForm(button, form) {
    button.addEventListener('click', function() {
        if (form.style.display === 'none' || form.style.display === '') {
            form.style.display = 'flex';
        } else {
            form.style.display = 'none';
        }
    });
    
    const submitBtn = form.querySelector('button[type="button"]');
    if (submitBtn) {
        submitBtn.addEventListener('click', async function(e) {
            e.preventDefault();
            
            const nameInput = form.querySelector('input[name="name"]');
            const startInput = form.querySelector('input[name="start"]');
            const endInput = form.querySelector('input[name="end"]');
            
            if (!nameInput.value.trim() || !startInput.value || !endInput.value) {
                updateStatusMessage('Please fill in all calendar fields', 'error');
                return;
            }
            
            const startDate = new Date(startInput.value);
            const endDate = new Date(endInput.value);
            
            if (endDate <= startDate) {
                updateStatusMessage('End time must be after start time', 'error');
                return;
            }
            
            const originalText = submitBtn.textContent;
            submitBtn.textContent = 'Adding...';
            submitBtn.disabled = true;
            
            const formData = new FormData();
            formData.append('name', nameInput.value);
            formData.append('start', startInput.value);
            formData.append('end', endInput.value);
            
            try {
                const response = await fetch('/add_to_calendar', {
                    method: 'POST',
                    body: formData
                });
                
                const result = await response.json();
                
                if (result.success) {
                    updateStatusMessage('Event added to calendar successfully!', 'success');
                    form.style.display = 'none';
                    nameInput.value = '';
                    startInput.value = '';
                    endInput.value = '';
                } else {
                    updateStatusMessage(result.message || 'Failed to add event to calendar', 'error');
                }
            } catch (error) {
                console.error('Error adding event to calendar:', error);
                updateStatusMessage('Error adding event to calendar', 'error');
            } finally {
                submitBtn.textContent = originalText;
                submitBtn.disabled = false;
            }
        });
    }
}

// Expand/Collapse functionality
document.addEventListener('DOMContentLoaded', function() {
    const expands = document.querySelectorAll('.expand');

    for (let i = 0; i < expands.length; i++){
        expands[i].addEventListener("click", () => {
            const emailIndex = expands[i].dataset.id;
            const emailElement = document.querySelector(`.email[data-id="${emailIndex}"]`);
            
            if (emailElement) {
                if (emailElement.style.display === "none" || emailElement.style.display === "") {
                    emailElement.style.display = "flex";
                    expands[i].textContent = "Collapse";
                } else {
                    emailElement.style.display = "none";
                    expands[i].textContent = "Expand";
                }
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
            
            if (!todoId || todoId === "") {
                updateStatusMessage('Error: No todo ID found', 'error');
                return;
            }
            
            const originalText = this.textContent;
            this.textContent = 'Removing...';
            this.disabled = true;
            
            try {
                const response = await fetch("/remove_todo", {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/json"
                    },
                    body: JSON.stringify({id: todoId})
                });
                
                const data = await response.json();
                
                if (data.success) {
                    const itemwrap = this.closest('.itemwrap');
                    const emailIndex = this.closest('.itemwrap').querySelector('.expand').dataset.id;
                    const emailDiv = document.querySelector(`.email[data-id="${emailIndex}"]`);
                    
                    if (itemwrap) {
                        itemwrap.style.transition = 'opacity 0.3s ease, transform 0.3s ease';
                        itemwrap.style.opacity = '0';
                        itemwrap.style.transform = 'translateX(20px)';
                        setTimeout(() => itemwrap.remove(), 300);
                    }
                    if (emailDiv) {
                        emailDiv.style.transition = 'opacity 0.3s ease';
                        emailDiv.style.opacity = '0';
                        setTimeout(() => emailDiv.remove(), 300);
                    }
                    
                    updateStatusMessage('Item removed successfully', 'success');
                    
                    // Update header count
                    setTimeout(() => {
                        const remainingTodos = document.querySelectorAll('.itemwrap[data-todo-id]:not([data-todo-id=""])').length - 1;
                        const header = document.querySelector('.summaryheader');
                        if (header) {
                            if (remainingTodos <= 0) {
                                header.textContent = 'All generated action items of the last 24 hours of emails (No action items found from recent emails.):';
                            } else {
                                header.textContent = `All generated action items of the last 24 hours of emails (Found ${remainingTodos} action items):`;
                            }
                        }
                    }, 350);
                } else {
                    updateStatusMessage(data.message || 'Error removing item', 'error');
                }
            } catch (error) {
                console.error('Error removing todo:', error);
                updateStatusMessage('Error removing item', 'error');
            } finally {
                this.textContent = originalText;
                this.disabled = false;
            }
        });
    }
});

// Reply functionality
document.addEventListener('DOMContentLoaded', function() {
    const reply_btns = document.querySelectorAll('.reply');
    
    reply_btns.forEach((btn, index) => {
        btn.addEventListener('click', () => {
            const emailContainer = btn.closest('.email');
            const replyDiv = emailContainer.querySelector('.replydiv');
            
            if (replyDiv) {
                if (btn.textContent === "Reply"){
                    replyDiv.classList.add('replycontain');
                    replyDiv.classList.remove('replywrap');
                    btn.textContent = "Hide Reply";
                } else {
                    replyDiv.classList.remove('replycontain');
                    replyDiv.classList.add('replywrap');
                    btn.textContent = "Reply";
                }
            }
        });
    });
});

// Reply submission functionality
document.addEventListener('DOMContentLoaded', function() {
    const reply_smts = document.querySelectorAll('.replysubmit');

    reply_smts.forEach((submitBtn, index) => {
        submitBtn.addEventListener('click', async (e) => {
            e.preventDefault();
            
            const emailContainer = submitBtn.closest('.email');
            const bodyInput = emailContainer.querySelector('.body');
            const ccInput = emailContainer.querySelector('.cc');
            const bccInput = emailContainer.querySelector('.bcc');
            
            const body = bodyInput ? bodyInput.value : '';
            const cc = ccInput ? ccInput.value : '';
            const bcc = bccInput ? bccInput.value : '';
            const subject = submitBtn.dataset.subject;
            const from = submitBtn.dataset.from;
            
            if (!body.trim()) {
                updateStatusMessage('Please enter a reply message', 'error');
                return;
            }
            
            const originalText = submitBtn.textContent;
            submitBtn.textContent = 'Sending...';
            submitBtn.disabled = true;
            
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
                
                const data = await response.json();
                
                if (data.success === true) {
                    if (bodyInput) bodyInput.value = "";
                    if (ccInput) ccInput.value = "";
                    if (bccInput) bccInput.value = "";
                    updateStatusMessage('Reply sent successfully', 'success');
                    
                    // Hide the reply form
                    const replyDiv = emailContainer.querySelector('.replydiv');
                    const replyBtn = emailContainer.querySelector('.reply');
                    if (replyDiv && replyBtn) {
                        replyDiv.classList.remove('replycontain');
                        replyDiv.classList.add('replywrap');
                        replyBtn.textContent = "Reply";
                    }
                } else {
                    updateStatusMessage(data.message || 'Error sending reply', 'error');
                }
            } catch (error) {
                console.error('Error sending reply:', error);
                updateStatusMessage('Error sending reply', 'error');
            } finally {
                submitBtn.textContent = originalText;
                submitBtn.disabled = false;
            }
        });
    });
});

// High priority functionality
document.addEventListener('DOMContentLoaded', function() {
    const markBtns = document.querySelectorAll('.mark');
    
    markBtns.forEach(btn => {
        btn.addEventListener('click', async function() {
            const sender = this.dataset.sender;
            const isAdding = this.dataset.add === "true";
            
            if (!sender) {
                updateStatusMessage('Error: No sender information found', 'error');
                return;
            }
            
            const originalText = this.textContent;
            this.textContent = 'Processing...';
            this.disabled = true;
            
            try {
                const response = await fetch('/mark_priority', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({
                        sender: sender,
                        add: isAdding
                    })
                });
                
                const data = await response.json();
                
                if (data.success) {
                    updateStatusMessage(data.message, 'success');
                    if (isAdding) {
                        this.textContent = 'Unmark Sender as High Priority';
                        this.dataset.add = "false";
                    } else {
                        this.textContent = 'Mark Sender as High Priority';
                        this.dataset.add = "true";
                    }
                } else {
                    updateStatusMessage(data.message || 'Error updating priority', 'error');
                }
            } catch (error) {
                console.error('Error updating priority:', error);
                updateStatusMessage('Error updating priority', 'error');
            } finally {
                this.disabled = false;
            }
        });
    });
});

// Initialize everything when page loads
document.addEventListener('DOMContentLoaded', function() {
    // Get all itemwraps that need processing (those with "Generating ..." text)
    const emailsToProcess = document.querySelectorAll('.itemwrap .item');
    const needsProcessing = Array.from(emailsToProcess).filter(item => 
        item.textContent.includes('Generating ...')
    ).map(item => item.closest('.itemwrap'));
    
    console.log(`Found ${needsProcessing.length} emails that need todo generation`);
    
    if (needsProcessing.length > 0) {
        // Start sequential processing immediately
        setTimeout(() => {
            processEmailsSequentially(needsProcessing);
        }, 500);
    } else {
        console.log('No emails need todo generation');
        // Update header to show final count
        const existingTodos = document.querySelectorAll('.itemwrap[data-todo-id]:not([data-todo-id=""])').length;
        updateHeaderCounts(existingTodos, existingTodos);
    }
});