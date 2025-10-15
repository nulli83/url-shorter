# Shorter: Minimalist URL Shortener

Shorter is a small, fast tool built with **Python/Flask** and **SQLite** to transform long URLs into clean, short links. The application focuses on **low latency** and **minimal dependencies**.

\<img width="1345" height="447" alt="preview" src="[https://github.com/user-attachments/assets/259270a6-f68d-4a2b-8665-dc61d9c4814b](https://github.com/user-attachments/assets/259270a6-f68d-4a2b-8665-dc61d9c4814b)" /\>

-----

##  Core Features

  * **URL Shortening:** Shortens all valid `http(s)` links.
  * **Custom Aliases:** Supports user-defined aliases (e.g., `/my-project`).
  * **Minimalist Database:** Uses built-in **SQLite** to manage all data, eliminating the need for an external database server.
  * **Statistics:** Tracks clicks (hits) for every shortened link.
  * **Dynamic QR:** Generates QR codes directly via an API endpoint.

-----

##  Architecture & Design Choices

Unlike many simple URL shorteners, Shorter uses a **reg-ex based router** within the Flask application (instead of standard decorators). This provides full control over URL matching and ensures a clean separation between application logic and Flask’s routing layer.

The code uses a random, collision-protected **UUID string** to generate short codes, instead of the more complex Base62 algorithms often found in similar projects.

-----

##  Installation & Running

**Prerequisites:** Python 3.8+

1.  Clone the repository:
    ```bash
    git clone https://github.com/nulli83/url-shorter.git
    cd url-shorter
    ```
2.  Install dependencies (including Flask and qrcode):
    ```bash
    pip install -r requirements.txt
    ```
3.  Start the application:
    ```bash
    python app.py
    ```
    *(The application starts on `http://127.0.0.1:8000` by default.)*
