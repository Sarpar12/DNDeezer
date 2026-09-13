import asyncio
import os

import httpx

from dndeezer.deezer.client import DeezerClient


async def main():
    arl = os.environ.get("DEEZER_ARL")
    if not arl:
        raise RuntimeError(
            "DEEZER_ARL is not set. Export your Deezer ARL before running this script."
        )

    async with httpx.AsyncClient() as http:
        client = DeezerClient(http, arl)

        # 1. Authenticate
        session = await client.authenticate()

        print(
            f"Authenticated: user={session.user_id} "
            f"country={session.country}"
        )

        # 2. Search albums
        print("\nAlbums:")
        albums = await client.search_albums(
            "Daft Punk",
            "Discovery",
        )

        for album in albums[:5]:
            print(
                album.id,
                album.artist.name,
                "-",
                album.title,
            )

        # 3. Fetch an album directly
        if albums:
            album = await client.get_album(albums[0].id)

            print("\nAlbum lookup:")
            print(album)

        # 4. Search tracks
        print("\nTracks:")
        tracks = await client.search_tracks(
            "Daft Punk",
            "Digital Love",
        )

        for track in tracks[:5]:
            print(
                track.id,
                track.artist.name,
                "-",
                track.title,
                f"({track.album_title})",
            )

        # 5. Fetch a track directly
        if tracks:
            track = await client.get_track(tracks[0].id)

            print("\nTrack lookup:")
            print(track)


asyncio.run(main())