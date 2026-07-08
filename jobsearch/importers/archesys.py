from django.conf import settings
import datetime
import requests
from bs4 import BeautifulSoup
from jobsearch.importers.utils import fetch_response

url = 'https://www.archesys.io/roles'

def get_jobs():
    # print("Importing ArcheSys")
    r = fetch_response('get', url, importer_name='ArcheSys', headers=settings.IMPORTER_HEADERS)
    if not r:
        return []
    soup = BeautifulSoup(r.content, "html.parser")
    wrapper = soup.find('div', class_='job_wrapper')
    if wrapper is None:
        return []
    job_cards = wrapper.find_all('div', class_="job_list")
    jobs = []
    for card in job_cards:
        a = card.find('a')
        if a is None or not a.get('href'):
            continue
        link = url + a['href'].replace('/roles', '')
        title = a.text.strip()
        loc = card.find('div', class_="text-size-small")
        location = loc.text.strip() if loc else ''
        jobs.append({
            'company': 'ArcheSys',
            'title': title,
            'link': link,
            'location': location,
            'job_id': link.rsplit('/')[-1],
            'pub_date': datetime.date.today()
        })
    return jobs



        