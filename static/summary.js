const expands = document.querySelectorAll('.expand');
const emails = document.querySelectorAll('.email');
const removes = document.querySelectorAll(".remove")

for (let i = 0; i < expands.length; i++){
    expands[i].addEventListener("click", () => {
        if (emails[i].style.display == "none") {
            emails[i].style.display = "flex";
            expands[i].textContent = "Collapse"
        }
        else {
            emails[i].style.display = "none";
            expands[i].textContent = "Expand";
        }
    })
}

for (let i = 0; i < removes.length; i++){
    removes[i].addEventListener("click", (function(index) {
        return function() {
            // Get the todo ID from the data attribute or from the emails array
            const todoId = removes[index].dataset.todoId; // Add data-todo-id to button
            
            fetch("/remove_todo", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json"
                },
                body: JSON.stringify({id: todoId})  // Send ID instead of index
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    const itemwrap = removes[index].closest('.itemwrap');
                    const emailDiv = document.querySelector(`.email[data-id="${index}"]`);
                    
                    if (itemwrap) {
                        itemwrap.remove();
                    }
                    if (emailDiv) {
                        emailDiv.remove();
                    }
                }
            })
            .catch(error => {
                console.error('Error removing todo:', error);
            });
        }
    })(i));
}

let reply_btns = document.querySelectorAll('.reply')
let reply_divs = document.querySelectorAll('.replydiv')
for (let i = 0; i < reply_btns.length; i++){
    reply_btns[i].textContent = "Reply"
    reply_btns[i].addEventListener('click', () => {
    if (reply_btns[i].textContent == "Reply"){
        reply_divs[i].classList.add('replycontain');
        reply_divs[i].classList.remove('replywrap');
        reply_btns[i].textContent = "Hide Reply"
    }
    else{
        reply_divs[i].classList.remove('replycontain');
        reply_divs[i].classList.add('replywrap');
        reply_btns[i].textContent = "Reply"
    }
})
}

let reply_smts = document.querySelectorAll('.replysubmit');
let bodys = document.querySelectorAll(".body");
let ccs = document.querySelectorAll(".cc");
let bccs = document.querySelectorAll('.bcc');
for (let i = 0; i < reply_smts.length; i++){
    reply_smts[i].addEventListener('click', (e) => {
    e.preventDefault()
    let body = bodys[i].value
    let cc = ccs[i].value
    let bcc = bccs[i].value
    let subject = reply_smts[i].dataset.subject
    let from = reply_smts[i].dataset.from
    console.log(body, cc, bcc, subject, from)
    fetch("/reply", {
        method: "POST", 
        headers:{
            "Content-Type": "application/json"
        },
        body: JSON.stringify({from: from, subject: subject,
            cc: cc, bcc: bcc, body: body})
    })
    .then(response => response.json())
    .then(data => {
        if (data.success === true) {
            bodys[i].value = ""
            ccs[i].value = ""
            bccs[i].value = ""
            alert("reply sent successfully")
        }
    }
        
    )})
}

function setupStaticCalendarForms() {
    // Find all existing calendar buttons and forms
    const calendarBtns = document.querySelectorAll('.calendarbtn');
    
    calendarBtns.forEach(btn => {
        // Find the corresponding form (should be the next sibling)
        const calendar_form = btn.nextElementSibling;
        
        if (calendar_form && calendar_form.classList.contains('calendarform')) {
            // Add event listener to toggle form visibility
            btn.addEventListener("click", function() {
                if (calendar_form.style.display === "none" || calendar_form.style.display === "") {
                    calendar_form.style.display = "flex";
                } else {
                    calendar_form.style.display = "none";
                }
            });
            
            // Find the submit button within this form
            const submitBtn = calendar_form.querySelector('button[type="button"]');
            
            if (submitBtn) {
                submitBtn.addEventListener("click", async function(e) {
                    e.preventDefault();
                    
                    // Get form inputs
                    const nameInput = calendar_form.querySelector('input[name="name"]');
                    const startInput = calendar_form.querySelector('input[name="start"]');
                    const endInput = calendar_form.querySelector('input[name="end"]');
                    
                    // Collect form data
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
                        
                        if (result.success === true) {
                            alert('Event was successfully added to the calendar!');
                            // Hide the form after successful submission
                            calendar_form.style.display = "none";
                            // Clear the form
                            nameInput.value = '';
                            startInput.value = '';
                            endInput.value = '';
                        } else {
                            alert('Failed to add event to calendar. Please try again.');
                        }
                    } catch (error) {
                        console.error('Error adding event to calendar:', error);
                        alert('An error occurred while adding the event. Please try again.');
                    }
                });
            }
        }
    });
}

// Call this function when the page loads to setup existing forms
document.addEventListener('DOMContentLoaded', setupStaticCalendarForms);
