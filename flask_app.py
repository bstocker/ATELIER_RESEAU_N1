from flask import Flask

app = Flask(__name__)

@app.get("/")
def hello():
    return "Bonjour tout le monde ! (Flask on PythonAnywhere)"

if __name__ == "__main__":
    # utile en local uniquement
    app.run(host="0.0.0.0", port=5000, debug=True)
