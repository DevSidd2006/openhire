const colors = ["red", "green", "blue", "yellow"];
let counter = 0;

setInterval(() => {
    document.body.style.backgroundColor = colors[counter++ % colors.length];
}, 1000);