const unsubs = document.querySelectorAll('.unsub');
for (let i = 0; i < unsubs.length; i++){
    unsubs[i].addEventListener("click", () => {
        console.log(unsubs[i].dataset.sender)
        fetch("/remove_unsubscribe", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({ sender: unsubs[i].dataset.sender })
        })
        .then(response => response.json())
        .then(data => {
            if (data.status === "success") {
                const row = unsubs[i].closest("tr");
                if (row) row.remove();
                                const remainingRows = document.querySelectorAll("table tr").length;
                if (remainingRows <= 1) {
                    document.querySelector(".unsubwrapper").innerHTML = "<p>No unsubscribes left.</p>";
                }
            }

        })
    })
}