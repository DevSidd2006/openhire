<script>
colors = ["red", "green", "blue", "yellow"]

counter = 0

setInterval(() => {
    document.body.style.backgroundColor = colors[counter++]
}, 1000)

</script>