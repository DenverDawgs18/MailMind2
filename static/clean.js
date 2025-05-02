document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll(".delete").forEach(button => {
        button.addEventListener("click", function () {
            const sender = this.dataset.sender;
            deleteSender(sender);
        });
    });
});




async function deleteSender(sender) {
    const response = await fetch('/delete_sender', {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sender_name: sender })
    });

    const data = await response.json();
    if (data.error) {
        alert(data.error);
        return;
    }

    updateSendersList(data.senders);
    console.log(data.deleted)
    updateRequestedList(data.deleted);
}

document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll(".restore").forEach(button => {
        button.addEventListener("click", function () {
            const sender = this.dataset.sender;
            restoreSender(sender);
        });
    });
});

async function restoreSender(sender) {
    console.log(sender);
    const response = await fetch('/restore_sender', {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sender_name: sender })
    });

    const data = await response.json();
    if (data.error) {
        alert(data.error);
        return;
    }

    updateSendersList(data.senders);
    updateRequestedList(data.deleted)
}

function updateSendersList(senders) {
    const senderList = document.querySelector(".senders");
    senderList.innerHTML = ""; 

    senders.forEach(([emailSender, count]) => {
        const wrapper = document.createElement("div");
        wrapper.classList.add("senderwrapper");

        const senderName = document.createElement("p");
        senderName.classList.add("sender");
        senderName.textContent = emailSender;

        const senderCount = document.createElement("p");
        senderCount.classList.add("number");
        senderCount.textContent = count;

        const deleteButton = document.createElement("button");
        deleteButton.classList.add("delete");
        deleteButton.textContent = "Delete";
        deleteButton.dataset.sender = emailSender;
        deleteButton.addEventListener("click", () => deleteSender(emailSender));

        wrapper.appendChild(senderName);
        wrapper.appendChild(senderCount);
        wrapper.appendChild(deleteButton);

        senderList.appendChild(wrapper);
    });
}

function updateRequestedList(senders) {
    console.log(senders)
    const requested = document.querySelector('.reqwrap');
    requested.innerHTML = ""
    senders.forEach(sender => {
        console.log(sender)
        const div = document.createElement('div');
        div.classList.add('reqcont');
    
        const p = document.createElement('p');
        p.classList.add('req');
        p.textContent = sender;
        div.appendChild(p);
    

    
        const button = document.createElement('button');
        button.type = 'submit';
        button.classList.add('restore');
        button.setAttribute('data-sender', sender);
        button.textContent = 'Restore';
        button.addEventListener("click", function () {
            const sender = this.dataset.sender;
            restoreSender(sender);
        });
        div.appendChild(button)
    
        requested.appendChild(div);
    }) 
}
