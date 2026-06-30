import os
import re
import json
import queue
import threading
import subprocess
import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, jsonify
import yt_dlp

app = Flask(__name__)

CACHE_FILE = 'download_cache.json'

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_to_cache(track_title, file_path):
    cache = load_cache()
    cache[track_title] = {
        'file_path': file_path,
        'status': 'completed'
    }
    try:
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"[Cache Error] Failed to write to disk: {e}")

# --- BACKGROUND QUEUE SYSTEM ---
download_queue = queue.Queue()
queue_status = {
    'active': False,
    'total': 0,
    'completed': 0,
    'failed': 0,
    'skipped': 0,
    'current_item': ""
}

def process_queue():
    global queue_status
    queue_status['active'] = True
    
    while not download_queue.empty():
        track, save_path = download_queue.get()
        track_title = track['title']
        queue_status['current_item'] = track_title
        
        # Check cache before doing any heavy lifting
        cache = load_cache()
        if track_title in cache and os.path.exists(cache[track_title]['file_path']):
            print(f"[Cache Match] Skipping {track_title}, already downloaded.")
            queue_status['skipped'] += 1
            download_queue.task_done()
            continue

        print(f"[Queue Log] Starting download: {track_title}...")
        success = False
        target_file = ""
        
        try:
            if not os.path.exists(save_path):
                os.makedirs(save_path, exist_ok=True)
                
            if track['platform'] == 'spotify':
                cmd = ['spotdl', 'download', track['url'], '--output', f"{save_path}/{{title}} - {{artist}}.{{ext}}"]
                result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                success = (result.returncode == 0)
                # Spotdl structure tracking reference
                target_file = os.path.join(save_path, f"{track_title}.mp3") 
            else:
                ydl_opts = {
                    'format': 'bestaudio/best',
                    'outtmpl': f"{save_path}/%(title)s.%(ext)s",
                    'postprocessors': [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': '192',
                    }],
                    'quiet': True
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(track['url'], download=True)
                    target_file = ydl.prepare_filename(info).rsplit('.', 1)[0] + ".mp3"
                success = True
        except Exception as e:
            print(f"[Queue Error] Processing failed for {track_title}: {e}")
            success = False

        if success:
            queue_status['completed'] += 1
            save_to_cache(track_title, target_file)
        else:
            queue_status['failed'] += 1
            
        download_queue.task_done()

    queue_status['active'] = False
    queue_status['current_item'] = "Finished"

def start_queue_worker():
    if not queue_status['active']:
        threading.Thread(target=process_queue, daemon=True).start()


# --- NO-API SPOTIFY PLAYLIST PARSER ---
def fetch_spotify_no_api(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        raise Exception(f"Failed to fetch Spotify page. HTTP status: {response.status_code}")

    soup = BeautifulSoup(response.text, 'html.parser')
    tracks = []
    
    script_tag = soup.find('script', type='application/ld+json')
    if script_tag:
        import json
        data = json.loads(script_tag.string)
        if 'track' in data:
            for item in data['track']:
                tracks.append({
                    'title': f"{item['name']} - {item['byArtist']['name']}",
                    'url': item['url'],
                    'platform': 'spotify'
                })
                
    if not tracks:
        for row in soup.find_all('span', dir='auto'):
            title = row.get_text().strip()
            if title and len(title) > 2 and not title.startswith(('http', 'Spotify', 'Sign Up')):
                tracks.append({
                    'title': title,
                    'url': url,
                    'platform': 'spotify'
                })
                
    seen = set()
    unique_tracks = []
    for t in tracks:
        if t['title'] not in seen:
            seen.add(t['title'])
            unique_tracks.append(t)
            
    return unique_tracks


# --- FLASK ROUTES ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/fetch-playlist', methods=['POST'])
def fetch_playlist():
    data = request.json
    url = data.get('url')
    platform = data.get('platform')
    
    try:
        if platform == 'youtube':
            tracks = []
            ydl_opts = {'extract_flat': True, 'quiet': True}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                playlist_dict = ydl.extract_info(url, download=False)
                if 'entries' in playlist_dict:
                    for entry in playlist_dict['entries']:
                        tracks.append({
                            'title': entry.get('title'),
                            'url': f"https://www.youtube.com/watch?v={entry.get('id')}",
                            'platform': 'youtube'
                        })
            return jsonify({'success': True, 'tracks': tracks})
            
        elif platform == 'spotify':
            tracks = fetch_spotify_no_api(url)
            if not tracks:
                return jsonify({'success': False, 'error': "No tracks found."})
            return jsonify({'success': True, 'tracks': tracks})
            
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/add-to-queue', methods=['POST'])
def add_to_queue():
    data = request.json
    tracks = data.get('tracks', [])
    save_path = data.get('save_path', '').strip()

    if not save_path:
        return jsonify({'success': False, 'error': "Target directory string path missing."})

    global queue_status
    if not queue_status['active'] and download_queue.empty():
        queue_status['total'] = 0
        queue_status['completed'] = 0
        queue_status['failed'] = 0
        queue_status['skipped'] = 0

    for track in tracks:
        download_queue.put((track, save_path))
        queue_status['total'] += 1

    start_queue_worker()
    return jsonify({'success': True, 'queued_count': len(tracks)})

@app.route('/queue-status', methods=['GET'])
def get_queue_status():
    global queue_status
    return jsonify({
        'active': queue_status['active'],
        'total': queue_status['total'],
        'completed': queue_status['completed'],
        'failed': queue_status['failed'],
        'skipped': queue_status['skipped'],
        'current_item': queue_status['current_item'],
        'remaining': download_queue.qsize()
    })

if __name__ == '__main__':
    app.run(debug=True, port=5050)
