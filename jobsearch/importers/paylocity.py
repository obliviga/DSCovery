import json
import pytz
import re
import requests
from jobsearch.importers.utils import fetch_response

from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from django.conf import settings

root_url = 'https://recruiting.paylocity.com'

# Name, key
firms = [
    ('eSimplicity', 'a2d790ab-c239-40b9-a6ea-9e5853bbd737/eSimplicity'),
    ('So Company', '1974d707-52df-497d-9c10-b664aec386a3/Storij-Inc-Current-Openings'),
    ('Vaultes', '512b109e-bb46-4419-98e4-84028b520a50/Vaultes-LLC/'),
    ('Snowbird Agility', '7f253d89-50c7-45db-981f-2eb478344672/Snowbird-Agility'),
]


def get_jobs():
    jobs = []
    for firm in firms:    
        co_name, key = firm
        # print("Importing", co_name)
        url = root_url + '/recruiting/jobs/All/' + key

        r = fetch_response('get', url, importer_name=co_name, headers=settings.IMPORTER_HEADERS)
        if not r:
            continue
        soup = BeautifulSoup(r.content, "html.parser")
        script_tag = soup.find('script', text=re.compile(r'window\.pageData\s*=\s*'))

        if script_tag:
            # Extract the JavaScript content and use regular expression to extract JSON
            script_content = script_tag.string
            match = re.search(r'window\.pageData\s*=\s*(\{.*\});', script_content, re.DOTALL)

            if match: # Parse the JSON data
                app_data = json.loads(match.group(1))
                job_cards = app_data.get('Jobs', [])

                for card in job_cards:
                    try:
                        raw = card.get('PublishedDate')
                        pub = None
                        if raw:
                            pub = datetime.fromisoformat(raw.replace('Z', '+00:00'))
                        if pub and pub.tzinfo is None:
                            pub = pub.replace(tzinfo=pytz.utc)
                        jobs.append({
                            'company': co_name,
                            'title': card.get('JobTitle', ''),
                            'link': root_url + "/Recruiting/Jobs/Details/" + str(card.get('JobId', '')),
                            'location': card.get('LocationName', ''),
                            'job_id': card.get('JobId', ''),
                            'pub_date': pub
                        })
                    except Exception:
                        continue
    return jobs
 