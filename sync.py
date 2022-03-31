#!/usr/bin/env python3

import argparse
from auth import open_tidal_session, open_spotify_session
from functools import partial
from multiprocessing import Pool
import sys
import spotipy
import tidalapi
from tidalapi_patch import create_tidal_playlist, set_tidal_playlist
import time
from tqdm import tqdm
import unicodedata
import webbrowser
import yaml

def normalize(s):
    return unicodedata.normalize('NFD', s).encode('ascii', 'ignore').decode('ascii')

def simple(input_string):
    # only take the first part of a string before any hyphens or brackets to account for different versions
    return input_string.split('-')[0].strip().split('(')[0].strip().split('[')[0].strip()

def duration_match(tidal_track, spotify_track, tolerance=2):
    # the duration of the two tracks must be the same to within 2 seconds
    return abs(tidal_track.duration - spotify_track['duration_ms']/1000) < tolerance

def name_match(tidal_track, spotify_track):
    def exclusion_rule(pattern, tidal_track, spotify_track):
        spotify_has_pattern = pattern in spotify_track['name'].lower()
        tidal_has_pattern = pattern in tidal_track.name.lower() or (not tidal_track.version is None and (pattern in tidal_track.version.lower()))
        return spotify_has_pattern != tidal_has_pattern

    # handle some edge cases
    if exclusion_rule("instrumental", tidal_track, spotify_track): return False
    if exclusion_rule("acapella", tidal_track, spotify_track): return False
    if exclusion_rule("remix", tidal_track, spotify_track): return False

    # the simplified version of the Spotify track name must be a substring of the Tidal track name
    # Try with both un-normalized and then normalized
    simple_spotify_track = simple(spotify_track['name'].lower()).split('feat.')[0].strip()
    return simple_spotify_track in tidal_track.name.lower() or normalize(simple_spotify_track) in normalize(tidal_track.name.lower())

def artist_match(tidal_track, spotify_track):
    def split_artist_name(artist):
       if '&' in artist:
           return artist.split('&')
       elif ',' in artist:
           return artist.split(',')
       else:
           return [artist]

    def get_tidal_artists(tidal_track, do_normalize=False):
        result = []
        for artist in tidal_track.artists:
            if do_normalize:
                artist_name = normalize(artist.name)
            else:
                artist_name = artist.name
            result.extend(split_artist_name(artist_name))
        return set([simple(x.strip().lower()) for x in result])

    def get_spotify_artists(spotify_track, do_normalize=False):
        result = []
        for artist in spotify_track['artists']:
            if do_normalize:
                artist_name = normalize(artist['name'])
            else:
                artist_name = artist['name']
            result.extend(split_artist_name(artist_name))
        return set([simple(x.strip().lower()) for x in result])
    # There must be at least one overlapping artist between the Tidal and Spotify track
    # Try with both un-normalized and then normalized
    if get_tidal_artists(tidal_track).intersection(get_spotify_artists(spotify_track)) != set():
        return True
    return get_tidal_artists(tidal_track, True).intersection(get_spotify_artists(spotify_track, True)) != set()
  
def match(tidal_track, spotify_track):
    return duration_match(tidal_track, spotify_track) and name_match(tidal_track, spotify_track) and artist_match(tidal_track, spotify_track)

def tidal_search(spotify_track_and_cache, tidal_session):
    spotify_track, cached_tidal_track = spotify_track_and_cache
    if cached_tidal_track: return cached_tidal_track
    # search for album name and first album artist
    if 'album' in spotify_track and 'artists' in spotify_track['album'] and len(spotify_track['album']['artists']):
        album_result = tidal_session.search('album', simple(spotify_track['album']['name']) + " " + simple(spotify_track['album']['artists'][0]['name']))
        for album in album_result.albums:
            album_tracks = tidal_session.get_album_tracks(album.id)
            if len(album_tracks) >= spotify_track['track_number']:
                track = album_tracks[spotify_track['track_number'] - 1]
                if match(track, spotify_track):
                    return track
    # if that fails then search for track name and first artist
    for track in tidal_session.search('track', simple(spotify_track['name']) + ' ' + simple(spotify_track['artists'][0]['name'])).tracks:
        if match(track, spotify_track):
            return track

def get_tidal_playlists_dict(tidal_session):
    # a dictionary of name --> playlist
    tidal_playlists = tidal_session.get_user_playlists(tidal_session.user.id)
    output = {}
    for playlist in tidal_playlists:
        output[playlist.name] = playlist
    return output 

def repeat_on_exception(function, *args, remaining=5, **kwargs):
    # utility to repeat calling the function up to 5 times if an exception is thrown
    try:
        return function(*args, **kwargs)
    except:
        if remaining:
            print("Error, retrying {} more times".format(remaining))
        else:
            print("Repeated error calling the function '{}' with the following arguments:".format(function.__name__))
            print(args)
            raise
        time.sleep(5)
        return repeat_on_exception(function, *args, remaining=remaining-1, **kwargs)

def _enumerate_wrapper(value_tuple, function, **kwargs):
    # just a wrapper which accepts a tuple from enumerate and returns the index back as the first argument
    index, value = value_tuple
    return (index, repeat_on_exception(function, value, **kwargs))

def call_async_with_progress(function, values, description, num_processes, **kwargs):
    results = len(values)*[None]
    with Pool(processes=num_processes) as process_pool:
        for index, result in tqdm(process_pool.imap_unordered(partial(_enumerate_wrapper, function=function, **kwargs),
                                  enumerate(values)), total=len(values), desc=description):
            results[index] = result
    return results

def get_tracks_from_spotify_playlist(spotify_session, spotify_playlist):
    output = []
    results = spotify_session.playlist_tracks(spotify_playlist['id'], fields="next,items(track(name,album(name,artists),artists,track_number,duration_ms,id))")
    while True:
        output.extend([r['track'] for r in results['items'] if r['track'] is not None])
        # move to the next page of results if there are still tracks remaining in the playlist
        if results['next']:
            results = spotify_session.next(results)
        else:
            return output

class TidalPlaylistCache:
    def __init__(self, playlist, tidal_session):
        self._data = tidal_session.get_playlist_tracks(playlist.id)

    def _search(self, spotify_track):
        ''' check if the given spotify track was already in the tidal playlist.'''
        results = []
        for tidal_track in self._data:
            if match(tidal_track, spotify_track):
                return tidal_track
        return None

    def search(self, spotify_session, spotify_playlist):
        ''' Add the cached tidal track where applicable to a list of spotify tracks '''
        results = []
        cache_hits = 0
        work_to_do = False
        spotify_tracks = get_tracks_from_spotify_playlist(spotify_session, spotify_playlist)
        for track in spotify_tracks:
            cached_track = self._search(track)
            if cached_track:
                results.append( (track, cached_track) )
                cache_hits += 1
            else:
                results.append( (track, None) )
        return (results, cache_hits)

def tidal_playlist_is_dirty(tidal_session, playlist_id, new_track_ids):
    old_tracks = tidal_session.get_playlist_tracks(playlist_id)
    if len(old_tracks) != len(new_track_ids):
        return True
    for i in range(len(old_tracks)):
        if old_tracks[i].id != new_track_ids[i]:
            return True
    return False

def sync_playlist(spotify_session, tidal_session, spotify_id, tidal_id, config):
    try:
        spotify_playlist = spotify_session.playlist(spotify_id)
    except spotipy.SpotifyException as e:
        print("Error getting Spotify playlist " + spotify_id)
        print(e)
        results.append(None)
        return
    if tidal_id:
        # if a Tidal playlist was specified then look it up
        try:
            tidal_playlist = tidal_session.get_playlist(tidal_id)
        except exception:
            print("Error getting Tidal playlist " + tidal_id)
            print(e)
            return
    else:
        # create a new Tidal playlist if required
        tidal_playlist = create_tidal_playlist(tidal_session, spotify_playlist['name'])
    tidal_track_ids = []
    spotify_tracks, cache_hits = TidalPlaylistCache(tidal_playlist, tidal_session).search(spotify_session, spotify_playlist)
    if cache_hits == len(spotify_tracks):
        print("No new tracks to search in Spotify playlist '{}'".format(spotify_playlist['name']))
        return

    task_description = "Searching Tidal for {}/{} tracks in Spotify playlist '{}'".format(len(spotify_tracks) - cache_hits, len(spotify_tracks), spotify_playlist['name'])
    tidal_tracks = call_async_with_progress(tidal_search, spotify_tracks, task_description, config.get('subprocesses', 50), tidal_session=tidal_session)
    for index, tidal_track in enumerate(tidal_tracks):
        spotify_track = spotify_tracks[index][0]
        if tidal_track:
            tidal_track_ids.append(tidal_track.id)
        else:
            color = ('\033[91m', '\033[0m')
            print(color[0] + "Could not find track {}: {} - {}".format(spotify_track['id'], ",".join([a['name'] for a in spotify_track['artists']]), spotify_track['name']) + color[1])

    if tidal_playlist_is_dirty(tidal_session, tidal_playlist.id, tidal_track_ids):
        set_tidal_playlist(tidal_session, tidal_playlist.id, tidal_track_ids)
    else:
        print("No changes to write to Tidal playlist")

def sync_list(spotify_session, tidal_session, playlists, config):
  results = []
  for spotify_id, tidal_id in playlists:
    # sync the spotify playlist to tidal
    repeat_on_exception(sync_playlist, spotify_session, tidal_session, spotify_id, tidal_id, config)
    results.append(tidal_id)
  return results

def pick_tidal_playlist_for_spotify_playlist(spotify_playlist, tidal_playlists):
    if spotify_playlist['name'] in tidal_playlists:
      # if there's an existing tidal playlist with the name of the current playlist then use that
      tidal_playlist = tidal_playlists[spotify_playlist['name']]
      return (spotify_playlist['id'], tidal_playlist.id)
    else:
      print(f"No playlist found on Tidal corresponding to Spotify playlist: '{spotify_playlist['name']}', creating new playlist")
      return (spotify_playlist['id'], None)
    

def get_user_playlist_mappings(spotify_session, tidal_session, config):
  results = []
  spotify_playlists = get_playlists_from_spotify(spotify_session, config)
  tidal_playlists = get_tidal_playlists_dict(tidal_session)
  for spotify_playlist in spotify_playlists:
      results.append( pick_tidal_playlist_for_spotify_playlist(spotify_playlist, tidal_playlists) )
  return results

def get_playlists_from_spotify(spotify_session, config):
    # get all the user playlists from the Spotify account
    playlists = []
    spotify_results = spotify_session.user_playlists(config['spotify']['username'])
    exclude_list = set([x.split(':')[-1] for x in config.get('excluded_playlists', [])])
    while True:
        for spotify_playlist in spotify_results['items']:
            playlists.append(spotify_playlist)
        # move to the next page of results if there are still playlists remaining
        if spotify_results['next']:
            spotify_results = spotify_session.next(spotify_results)
        else:
            break
    return playlists

def get_playlists_from_config(config):
    # get the list of playlist sync mappings from the configuration file
    return [(item['spotify_id'], item['tidal_id']) for item in config['sync_playlists']]

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='config.yml', help='location of the config file')
    parser.add_argument('--uri', help='synchronize a specific URI instead of the one in the config')
    args = parser.parse_args()

    with open(args.config, 'r') as f:
        config = yaml.safe_load(f)
    spotify_session = open_spotify_session(config['spotify'])
    tidal_session = open_tidal_session()
    if not tidal_session.check_login():
        sys.exit("Could not connect to Tidal")
    if args.uri:
        # if a playlist ID is explicitly provided as a command line argument then use that
        spotify_playlist = spotify_session.playlist(args.uri)
        tidal_playlists = get_tidal_playlists_dict(tidal_session)
        tidal_playlist = pick_tidal_playlist_for_spotify_playlist(spotify_playlist, tidal_playlists)
        sync_list(spotify_session, tidal_session, [tidal_playlist], config)
    elif config.get('sync_playlists', None):
        # if the config contains a sync_playlists list of mappings then use that
        sync_list(spotify_session, tidal_session, get_playlists_from_config(config), config)
    else:
        # otherwise just use the user playlists in the Spotify account
        sync_list(spotify_session, tidal_session, get_user_playlist_mappings(spotify_session, tidal_session, config), config)
