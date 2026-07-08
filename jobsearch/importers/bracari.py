from django.conf import settings
import datetime
import requests
from bs4 import BeautifulSoup
from jobsearch.importers.utils import fetch_response

company = "Bracari"
root_url = 'https://www.bracari.com'
url = root_url + '/join-us'

def get_jobs():
    # print("Importing", company)
    r = fetch_response('get', url, importer_name=company, headers=settings.IMPORTER_HEADERS)
    if not r:
        return []
    soup = BeautifulSoup(r.content, "html.parser")

    job_cards = soup.find_all('a', class_="career-card")
    jobs = []
    for card in job_cards:
        title_el = card.find('div', class_="text-bold")
        if not title_el:
            continue
        title = title_el.text.strip()
        href = card.get('href')
        if not href:
            continue
        link = root_url + href
        loc_el = card.find('div', class_="text-gray-1")
        location = loc_el.text.strip() if loc_el else ''
        jobs.append({
            'company': company,
            'job_id': link.rstrip('/').rsplit('/', 1)[-1].split('?')[0].split('#')[0],
            'title': title,
            'link': link,
            'location': location,
            'pub_date': datetime.date.today()
        })
    return jobs
 