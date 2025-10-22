// Sequential action item generation for summary page
let isGenerating = false;

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

// Sequential processing of action items (adapted from emails.js)
async function processActionItemsSequentially() {
    if (isGenerating) return;
    
    isGenerating = true;
    const itemWraps = document.querySelectorAll('.itemwrap');
    let processedCount = 0;
    let totalToProcess = 0;
    
    // Count items that need processing
    for (let itemWrap of itemWraps) {
        const itemText = itemWrap.querySelector('.item');
        if (itemText && itemText.textContent.includes('Generating ...')) {
            totalToProcess++;
        }
    }
    
    if (totalToProcess === 0) {
        isGenerating = false;
        return;
    }
    
    updateStatusMessage(`Processing ${totalToProcess} action items...`, 'status');
    
    for (let i = 0; i < itemWraps.length; i++) {
        const itemWrap = itemWraps[i];
        const itemText = itemWrap.querySelector('.item');
        
        if (!itemText || !itemText.textContent.includes('Generating ...')) {
            continue;
        }
        
        // Find the corresponding email data
        const emailDiv = itemWrap.nextElementSibling;
        if (!emailDiv) continue;
        
        const bodyElement = Array.from(emailDiv.querySelectorAll('.emailsub')).find(div => {
            const header = div.querySelector('h2.header');
            return header && header.textContent.trim() === 'Body';
        });

        const bodyText = bodyElement ? bodyElement.querySelector('p.etext').textContent : '';

        if (!bodyElement) continue;
        
        try {
            updateStatusMessage(`Processing item ${processedCount + 1} of ${totalToProcess}...`, 'status');
            
            
            const response = await fetch("/get_one_action", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json"
                },
                body: JSON.stringify({ 
                    body: bodyText, 
                    index: i 
                })
            });

            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }

            const data = await response.json();
            
            // Update the item text
            if (data.action_item && data.action_item.toLowerCase() !== "no action.") {
                itemText.innerHTML = `• ${data.action_item}`;
            }
            else{
                itemWrap.remove()
            }

            
            // Add calendar functionality if needed
            if (data.calendar === true) {
                const existingCalendarBtn = itemWrap.querySelector('.calendarbtn');
                if (!existingCalendarBtn) {
                    addCalendarButton(itemWrap, data.accounts);
                }
            }
            
            processedCount++;
            
        } catch (err) {
            console.error("Error processing action item:", err);
            itemText.innerHTML = "• Error generating action item";
        }
    }
    
    isGenerating = false;
    updateStatusMessage(`Completed processing ${processedCount} emails!`, 'success');
}

// Add calendar button and form to an item
function addCalendarButton(itemWrap, accounts) {
    const calendarBtn = document.createElement('button');
    calendarBtn.className = 'calendarbtn';
    calendarBtn.textContent = 'Add to Calendar';
    
    const calendarForm = document.createElement('form');
    calendarForm.className = 'calendarform';
    calendarForm.style.display = 'none';
    
    // Create form elements
    const nameLabel = document.createElement('label');
    nameLabel.setAttribute('for', 'name');
    nameLabel.textContent = 'Name:';
    
    const nameInput = document.createElement('input');
    nameInput.type = 'text';
    nameInput.name = 'name';
    nameInput.id = 'name';
    
    const startLabel = document.createElement('label');
    startLabel.setAttribute('for', 'start');
    startLabel.textContent = 'Start:';
    
    const startInput = document.createElement('input');
    startInput.type = 'datetime-local';
    startInput.id = 'start';
    startInput.name = 'start';
    
    const endLabel = document.createElement('label');
    endLabel.setAttribute('for', 'end');
    endLabel.textContent = 'End:';
    
    const endInput = document.createElement('input');
    endInput.type = 'datetime-local';
    endInput.name = 'end';
    endInput.id = 'end';
    
    const submitBtn = document.createElement('button');
    submitBtn.type = 'button';
    submitBtn.textContent = 'Submit';

    const selectLabel = document.createElement("label");
    selectLabel.setAttribute('for', 'account');
    selectLabel.textContent = 'Account'
    
    const select = document.createElement("select");
    select.id = "account"
    select.name = "account"

    // Append all elements to form
    for (let i = 0; i < accounts.length; i++) {
        let option = document.createElement("option");
        option.value = accounts[i].id;
        option.textContent = accounts[i].email;
        select.appendChild(option);
    }

    calendarForm.appendChild(select);
    calendarForm.appendChild(nameLabel);
    calendarForm.appendChild(nameInput);
    calendarForm.appendChild(startLabel);
    calendarForm.appendChild(startInput);
    calendarForm.appendChild(endLabel);
    calendarForm.appendChild(endInput);
    calendarForm.appendChild(submitBtn);
    
    // Add event listeners
    calendarBtn.addEventListener('click', function() {
        if (calendarForm.style.display === 'none') {
            calendarForm.style.display = 'flex';
        } else {
            calendarForm.style.display = 'none';
        }
    });
    
    submitBtn.addEventListener('click', async function(e) {
        e.preventDefault();
        
        if (!nameInput.value.trim() || !startInput.value || !endInput.value || !select.value) {
            updateStatusMessage('Please fill in all calendar fields', 'error');
            return;
        }
        
        const formData = new FormData();
        formData.append('name', nameInput.value);
        formData.append('start', startInput.value);
        formData.append('end', endInput.value);
        formData.append('account', select.value)
        
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
                calendarForm.style.display = 'none';
                nameInput.value = '';
                startInput.value = '';
                endInput.value = '';
                select.value = '';
            } else {
                updateStatusMessage('Failed to add event to calendar', 'error');
            }
        } catch (error) {
            console.error('Error adding event to calendar:', error);
            updateStatusMessage('Error adding event to calendar', 'error');
        }
    });
    
    // Insert calendar button and form
    itemWrap.appendChild(calendarBtn);
    itemWrap.appendChild(calendarForm);
}

// Initialize sequential processing when page loads
document.addEventListener('DOMContentLoaded', function() {
    // Check if there are items that need processing
    const itemsNeedingProcessing = document.querySelectorAll('.item');
    let needsProcessing = false;
    
    for (let item of itemsNeedingProcessing) {
        if (item.textContent.includes('Generating ...')) {
            needsProcessing = true;
            break;
        }
    }
    if (needsProcessing) {
        // Small delay to ensure page is fully loaded
        setTimeout(() => {
            processActionItemsSequentially();
        }, 100);
    }
});

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




// Calendar forms functionality for existing items
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
                    const select = calendar_form.querySelector("select")
                    
                    if (!nameInput.value.trim() || !startInput.value || !endInput.value || !select.value) {
                        updateStatusMessage('Please fill in all calendar fields', 'error');
                        return;
                    }
                    
                    const formData = new FormData();
                    formData.append('name', nameInput.value);
                    formData.append('start', startInput.value);
                    formData.append('end', endInput.value);
                    formData.append('account', select.value);
                    
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
                            select.value = '';
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