const unsubs = document.querySelectorAll('.unsub');

for (let i = 0; i < unsubs.length; i++) {
    // Add click handler to the parent <a> tag instead of the button
    const link = unsubs[i].closest("a");
    
    link.addEventListener("click", (e) => {
        const button = unsubs[i];
        const isManual = button.dataset.manual === 'true';
        
        if (!isManual) {
            // Prevent the link from opening when using Selenium
            e.preventDefault();
        }
        // If manual=true, let the link open normally (don't prevent default)
        
        button.textContent = "Attempting auto-unsubscribe";
        button.disabled = true;
        
        fetch("/remove_unsubscribe", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({ 
                sender: button.dataset.sender, 
                manual: button.dataset.manual 
            })
        })
        .then(response => response.json())
        .then(data => {
            if (data.status === "success") {
                document.querySelector(".unsubmsg").innerHTML = data.message + "<br>"
                document.querySelector(".unsubmsg").classList.add("msgenabled");
                document.querySelector(".unsubmsg").classList.remove("msgdisabled");
                const row = button.closest("div");
                if (row) row.remove();
                const remainingRows = document.querySelectorAll(".unsubwrapper").length;
                if (remainingRows <= 1) {
                    document.querySelector(".unsubmsg").innerHTML = "No unsubscribes left";
                }
            } else {
                // Selenium failed, switch to manual mode
                button.dataset.manual = "true";
                button.disabled = false;
                button.textContent = "Retry (Manual)"; // Optional: change button text
                // No need to change pointerEvents since we want the link to work now
                document.querySelector(".unsubmsg").innerHTML = data.message + "<br>";
                document.querySelector(".unsubmsg").classList.add("msgenabled");
                document.querySelector(".unsubmsg").classList.remove("msgdisabled");
            }
        });
    });
}

function updateSubnavOffset() {
  const navbar = document.querySelector(".navbar");
  const subnav = document.querySelector('.unsubmsg');
  const height = navbar.offsetHeight;
  subnav.style.top = `${height}px`;
}

document.addEventListener("DOMContentLoaded", function () {
    if (this.documentElement.clientWidth < 600) {
        document.querySelectorAll('.sender').forEach(el => {
            el.innerHTML = el.innerHTML.replace(/&lt;/, '<br><br>&lt;');
        });
    }
    updateSubnavOffset()
});

window.addEventListener("resize", updateSubnavOffset);