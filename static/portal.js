
const deleteHeaders = document.querySelectorAll(".deleteacct");
for (let i = 0; i < deleteHeaders.length; i++) {
    deleteHeaders[i].addEventListener("click", async e => {
        const res = await fetch('/delete_account', {
        method: 'POST',
        headers: {
            "Content-Type": "application/json"
        },
        body: JSON.stringify({id: deleteHeaders[i].children[0].dataset.accountid })
        });
        const data = await res.json();
        console.log(data)
        if (data[0].success){
            alert("Successfully removed account")
            deleteHeaders[i].parentElement.remove()
        }
        else{
            alert("Couldn't delete account")
        }

    })


}