const expands = document.querySelectorAll('.expand');
const emails = document.querySelectorAll('.email');

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