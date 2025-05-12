const expands = document.querySelectorAll('.expand');
const emails = document.querySelectorAll('.email');

for (let i = 0; i < expands.length; i++){
    expands[i].addEventListener("click", () => {
        if (emails[i].style.display == "none") {
            emails[i].style.display = "block";
            expands[i].textContent = "Collapse"
        }
        else {
            emails[i].style.display = "none";
            expands[i].textContent = "Expand";
        }
    })
}