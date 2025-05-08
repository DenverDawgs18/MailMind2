let mark_btn = document.querySelector('.mark')
document.addEventListener("DOMContentLoaded", () => {
   mark_btn.addEventListener('click', () => {
        let sender = mark_btn.getAttribute('data-sender')
        let add = mark_btn.getAttribute('add');
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
                    mark_btn.textContent = "Unmark Sender as High Priority"
                    mark_btn.setAttribute('add', "false")
                }
                else{
                    alert('Error marking')
                }
            })
        }
        else if (add === "false"){
            fetch("/unmark_high_priority", {
                method: "POS", 
                headers:{
                    "Content-Type": "application/json"
                },
                body: JSON.stringify({sender: sender})
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    mark_btn.textContent = "Mark Sender as High Priority";
                    mark_btn.setAttribute('add', "true");
                }
                else{
                    alert('Error marking');
                }
            })
        }
    })
})
let check = true
let reply_btn = document.querySelector('.reply')
reply_btn.addEventListener('click', () => {
    if (check){
        document.querySelector('#replydiv').classList.add('replycontain');
        document.querySelector('#replydiv').classList.remove('replywrap');
        reply_btn.textContent = "Hide Reply"
        check = false
    }
    else{
        document.querySelector('#replydiv').classList.remove('replycontain');
        document.querySelector('#replydiv').classList.add('replywrap');
        reply_btn.textContent = "Reply"
        check = true
    }

})
let reply_smt = document.querySelector('#replysubmit');
reply_smt.addEventListener('click', (e) => {
    e.preventDefault()
    let body = document.querySelector('#body').value;
    let cc = document.querySelector('#cc').value;
    let bcc = document.querySelector('#bcc').value;
    let subject = reply_smt.getAttribute('subject')
    let from = reply_smt.getAttribute('from')
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
            document.querySelector('#body').value = ""
            alert("reply sent successfully")
        }
    }
        
    )})
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

let loadMoreButton = document.querySelector('.loadmore');
let emailList = document.querySelector('.emails')

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

        if (currentActionItemDiv && body) {
            console.log(body.textContent);
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
            } catch (err) {
                console.error("Error fetching action item:", err);
            }
        }
    }
}

processEmailsSequentially(emails);

