let mark_btns = document.querySelectorAll('.mark')
document.addEventListener("DOMContentLoaded", () => {
   for (let i = 0; i < mark_btns.length; i++){
       mark_btns[i].addEventListener('click', () => {
        let sender = mark_btns[i].getAttribute('data-sender')
        let add = mark_btns[i].getAttribute('add');
        if (add === "true"){
            fetch("/mark_high_priority", {
                method: "POST", 
                headers:{
                    "Content-Type": "application/json"
                },
                body: JSON.stringify({sender: sender})
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    mark_btns[i].textContent = "Unmark Sender as High Priority"
                    mark_btns[i].setAttribute('add', "false")
                }
                else{
                    alert('Error marking')
                }
            })
        }
        else if (add === "false"){
            fetch("/unmark_high_priority", {
                method: "POST", 
                headers:{
                    "Content-Type": "application/json"
                },
                body: JSON.stringify({sender: sender})
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    mark_btns[i].textContent = "Mark Sender as High Priority";
                    mark_btns[i].setAttribute('add', "true");
                }
                else{
                    alert('Error marking');
                }
            })
        }
    })
   }
})
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

let compose = document.querySelector('.compose');
let dialog = document.querySelector('dialog');
let second_check = true;
compose.addEventListener('click', () =>{
    if (second_check){
        dialog.show();
        compose.textContent = "Hide";
        second_check = false;
    }
    else{
        dialog.close()
        compose.textContent = "Compose"
        second_check = true;
    }
})
let c_smt = document.querySelector("#csubmit");
c_smt.addEventListener('click', (e) => {
    e.preventDefault();
    let to = document.querySelector('#cto').value;
    let body = document.querySelector('#cbody').value;
    let cc = document.querySelector('#ccc').value;
    let bcc = document.querySelector('#cbcc').value;
    let subject = document.querySelector('#csubject').value;
    fetch("/send", {
        method: "POST", 
        headers:{
            "Content-Type": "application/json"
        },
        body: JSON.stringify({to: to, subject: subject, 
            body: body, cc: cc, bcc: bcc,})
    })
    .then(response => response.json())
    .then(data => {
        if (data.success === true) {
            document.querySelector('#cbody').value = ""
            document.querySelector('#cto').value = ""
            document.querySelector('#csubject').value = ""
            dialog.close()
            alert("Email sent successfully")
        }
    })});




/*
let loadMoreButton = document.querySelector('.loadmore');

loadMoreButton.addEventListener("click", () => {
        fetch("/load_more", {
            method: "POST",
            headers: {
                    "Content-Type": "application/json"
            }
        })
        .then(response => response.json())  // Convert response to JSON
        .then(data => {
            if (data.html) {
                const tempDiv = document.createElement("div"); // Temporary container
                tempDiv.innerHTML = data.html;

                // Append each email item individually for better control
                while (tempDiv.firstChild) {
                        emailList.appendChild(tempDiv.firstChild);
                }
            }
        })
        .catch(error => console.error("Error loading more emails:", error));
        });
*/

let emailList = document.querySelector('.emails')

let emails = emailList.children;
async function processEmailsSequentially(emails) {
    for (let j = 0; j < emails.length; j++) {
        const email = emails[j];
        const email_children = email.children;
        let currentActionItemDiv = null;
        let body = null;

        for (let i = 0; i < email_children.length; i++) {
            if (i === 2) {
                currentActionItemDiv = email_children[i].children[0];
            }
            if (i === 3) {
                body = email_children[i].children[1];
            }
        }
        console.log(currentActionItemDiv.children)
        if (currentActionItemDiv && body) {
            if (currentActionItemDiv.children.length === 1) {
                let p = document.createElement("p")
                p.textContent = "No action."
                currentActionItemDiv.appendChild(p)
            }
            if (currentActionItemDiv.children[1].textContent === "Generating ..."){
                try {
                    const response = await fetch("/get_one_action", {
                        method: "POST",
                        headers: {
                            "Content-Type": "application/json"
                        },
                        body: JSON.stringify({ body: body.textContent, index: j })
                    }); 

                    const data = await response.json();
                    currentActionItemDiv.children[1].textContent = data.action_item;
                    console.log(data.calendar)
                    if (data.calendar == true) {
                        const calendar_btn = document.createElement("button");
                        calendar_btn.classList.add("calendarbtn");
                        calendar_btn.textContent = "Add to Calendar";
                        
                        const calendar_form = document.createElement("form");
                        calendar_form.classList.add("calendarform");
                        calendar_form.style.display = "none";
                        
                        // Create form elements
                        const nameLabel = document.createElement("label");
                        nameLabel.setAttribute("for", "name");
                        nameLabel.textContent = "Name:";
                        
                        const nameInput = document.createElement("input");
                        nameInput.type = "text";
                        nameInput.name = "name";
                        nameInput.id = "name";
                        
                        const startLabel = document.createElement("label");
                        startLabel.setAttribute("for", "start");
                        startLabel.textContent = "Start:";
                        
                        const startInput = document.createElement("input");
                        startInput.type = "datetime-local";
                        startInput.id = "start";
                        startInput.name = "start";
                        
                        const endLabel = document.createElement("label");
                        endLabel.setAttribute("for", "end");
                        endLabel.textContent = "End:";
                        
                        const endInput = document.createElement("input");
                        endInput.type = "datetime-local";
                        endInput.name = "end";
                        endInput.id = "end";
                        
                        const submitBtn = document.createElement("button");
                        submitBtn.type = "button";
                        submitBtn.textContent = "Submit";
                        
                        // Append all elements to form
                        calendar_form.appendChild(nameLabel);
                        calendar_form.appendChild(nameInput);
                        calendar_form.appendChild(startLabel);
                        calendar_form.appendChild(startInput);
                        calendar_form.appendChild(endLabel);
                        calendar_form.appendChild(endInput);
                        calendar_form.appendChild(submitBtn);
                        submitBtn.addEventListener("click", async function(e) {
                            e.preventDefault();
                            
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
                                    // Optionally hide the form after successful submission
                                    calendar_form.style.display = "none";
                                    // Optionally clear the form
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
                        // Add event listener to toggle form visibility
                        calendar_btn.addEventListener("click", function() {
                            if (calendar_form.style.display === "none") {
                                calendar_form.style.display = "block";
                            } else {
                                calendar_form.style.display = "none";
                            }
                        });
                        currentActionItemDiv.appendChild(calendar_btn);
                        currentActionItemDiv.appendChild(calendar_form)
                    }
                } 
                catch (err) {
                    console.error("Error fetching action item:", err);
                }
            }
        }
    }
}
processEmailsSequentially(emails);

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

