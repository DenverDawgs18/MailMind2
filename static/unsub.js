let analyze = document.querySelector('.analyze');
let unsubList = document.querySelector('.unsubs')
analyze.addEventListener('click', () => {
    fetch("/analyze", {
        method: "POST", 
        headers:{
            "Content-Type": "application/json"
        },
        body: JSON.stringify({})
    })
    .then(response => response.json())  // Convert response to JSON
    .then(data => {
        if (data.html) {
            const tempDiv = document.createElement("div"); // Temporary container
            tempDiv.innerHTML = data.html;

            // Append each email item individually for better control
            while (tempDiv.firstChild) {
                    unsubList.appendChild(tempDiv.firstChild);
            }
        }
    })
    .catch(error => console.error("Error loading more emails:", error));
    });
