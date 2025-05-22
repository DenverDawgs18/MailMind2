document.addEventListener("DOMContentLoaded", function () {
    // Set up delete button event listeners
    document.querySelectorAll(".delete").forEach(button => {
        button.addEventListener("click", function () {
            const sender = this.dataset.sender;
            deleteSender(sender);
        });
    });

    // Set up restore button event listeners
    document.querySelectorAll(".restore").forEach(button => {
        button.addEventListener("click", function () {
            const sender = this.dataset.sender;
            restoreSender(sender);
        });
    });

    // Set up load more button event listener
    const loadMoreBtn = document.querySelector(".loadmore");
    const statsElement = document.querySelector('.stats');
    const loadAllBtn = document.querySelector(".loadall");
    
    if (loadMoreBtn) {
        loadMoreBtn.addEventListener("click", function() {
            // Disable button while loading
            loadMoreBtn.disabled = true;
            loadAllBtn.disabled = true;
            document.querySelectorAll(".restore").forEach(button => {
                const sender = button.dataset.sender;
                restoreSender(sender);
            });
            
            // Make AJAX request to load more emails
            fetch("/email_cleaner", {
                method: "POST",
                headers: {
                    'Content-Type': 'application/json',
                    'X-Requested-With': 'XMLHttpRequest'
                },
                body: JSON.stringify({all: "no"})
            })
            .then(response => response.json())
            .then(data => {
                // Update processed count in stats element
                if (statsElement) {
                    statsElement.textContent = `Loaded: ${data.processed_count} emails`;
                }
                
                // Update button state based on whether there are more emails to load
                loadMoreBtn.disabled = !data.has_more;
                loadAllBtn.disabled = !data.has_more
                
                // Update senders list with new data
                updateSendersList(data.text);
                
                // Check if there are any deleted senders to update
                if (data.deleted) {
                    updateRequestedList(data.deleted);
                }
            })
            .catch(error => {
                console.error('Error loading more emails:', error);
                alert('Error loading emails. Please try again.');
                
                // Re-enable button in case of error
                loadMoreBtn.disabled = false;
            });
        });
    }
    
    if (loadAllBtn) {
        loadAllBtn.addEventListener("click", function() {
            // Disable button while loading
            loadMoreBtn.disabled = true;
            loadAllBtn.disabled = true;
            document.querySelectorAll(".restore").forEach(button => {
                const sender = button.dataset.sender;
                restoreSender(sender);
            });
            
            // Make AJAX request to load more emails
            fetch("/email_cleaner", {
                method: "POST",
                headers: {
                    'Content-Type': 'application/json',
                    'X-Requested-With': 'XMLHttpRequest'
                },
                body: JSON.stringify({all: "yes"})
            })
            .then(response => response.json())
            .then(data => {
                // Update processed count in stats element
                if (statsElement) {
                    statsElement.textContent = `Loaded: ${data.processed_count} emails`;
                }
                
                // Update button state based on whether there are more emails to load
                
                // Update senders list with new data
                updateSendersList(data.text);
                
                // Check if there are any deleted senders to update
                if (data.deleted) {
                    updateRequestedList(data.deleted);
                }
            })
            .catch(error => {
                console.error('Error loading more emails:', error);
                alert('Error loading emails. Please try again.');
                
                // Re-enable button in case of error
                loadMoreBtn.disabled = false;
            });
        });
    }
    const removeBtn = document.querySelector('.remove')
    removeBtn.addEventListener("click", () => {
        fetch("/remove_all_senders", {
            method: "POST", 
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({})
        })
        .then(response => response.json())
        .then(data => {
            console.log(data.message)
            updateRequestedList([])
        })
    })
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
    console.log(data)

    updateSendersList(data.text);
    console.log(data.deleted);
    updateRequestedList(data.deleted);
}

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

    updateSendersList(data.text);
    updateRequestedList(data.deleted);
}

function updateSendersList(senders) {
    const senderList = document.querySelector(".senders");

    // Add title back first
    const title = document.createElement("h1");
    title.classList.add("reqhead");
    title.textContent = "Senders to Keep";

    senderList.innerHTML = ""; 
    senderList.appendChild(title);

    if (senders.length > 0) {
        // Sort by count descending before appending
        if (Array.isArray(senders[0])) {
            // Format: [[sender, count]]
            senders.sort((a, b) => b[1] - a[1]);
            senders.forEach(([emailSender, count]) => {
                addSenderToList(senderList, emailSender, count);
            });
        } else {
            // Format: [{sender, number}]
            senders.sort((a, b) => b.number - a.number);
            senders.forEach((item) => {
                addSenderToList(senderList, item.sender, item.number);
            });
        }
    }
}


function addSenderToList(senderList, emailSender, count) {
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
    deleteButton.textContent = "Add to Delete Pile";
    deleteButton.dataset.sender = emailSender;
    deleteButton.addEventListener("click", () => deleteSender(emailSender));

    wrapper.appendChild(senderName);
    wrapper.appendChild(senderCount);
    wrapper.appendChild(deleteButton);

    senderList.appendChild(wrapper);
}

function updateRequestedList(senders) {
    console.log(senders);
    const requested = document.querySelector('.reqwrap');
    
    if (!senders || senders.length === 0) {
        requested.innerHTML = "<h2 class='reqhead'>No Senders Requested to Delete</h2>";
        return;
    }
    
    requested.innerHTML = "";
    
    senders.forEach(sender => {
        console.log(sender);
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
        div.appendChild(button);
    
        requested.appendChild(div);
    });
}