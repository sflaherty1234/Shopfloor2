======================================
 Crye Precision Shopfloor Control
 Python Web App
======================================

REQUIREMENTS
------------
- Python 3.10 or later
- Flask (install with: pip install flask)


HOW TO RUN
----------
1. Open a terminal / command prompt
2. Navigate to this folder:
      cd "path\to\shopfloor_web"

3. Install Flask (one time):
      pip install flask

4. Start the server:
      python app.py

5. Open your browser and go to:
      http://localhost:5000

The app can be accessed from any device on the same network:
      http://<your-computer-ip>:5000


NOTES
-----
- Data is stored in memory only — it resets when the server restarts.
- Multiple users can connect simultaneously (operators on tablets,
  supervisor on a desktop, etc.).
- The page auto-refreshes state every 5 seconds.
