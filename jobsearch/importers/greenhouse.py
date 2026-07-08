from django.conf import settings
import datetime
import requests
from bs4 import BeautifulSoup

from jobsearch.importers.utils import already_in_jobs, fetch_response, map_section_to_practice

root_url = 'https://boards.greenhouse.io'

# Name, GH key
firms = [
    ('A1M', 'a1msolutions'),
    ('Agile Six', 'agilesix'),
    ('Aquia', 'aquia'),
    ('Bloom Works', 'bloomworks'),
    ('CivicActions', 'civicactions'),
    ('Nava', 'navapbc'),
    ('Oddball', 'oddball'),
    ('Raft', 'raft'),
]

def get_jobs():
    jobs = []
    # print("Importing greenhouse")
    for firm in firms:
        try:
            co_name, key = firm
            url = root_url + '/' + key

            r = fetch_response('get', url, importer_name=co_name, headers=settings.IMPORTER_HEADERS)

            if not r:
                url = 'https://job-boards.greenhouse.io/' + '/' + key
                r = fetch_response('get', url, importer_name=co_name, headers=settings.IMPORTER_HEADERS)
                if not r:
                    continue

            soup = BeautifulSoup(r.content, "html.parser")
            sections = soup.find_all('section', class_="level-0")
            # For some reason, sometimes Greenhouse outputs as a table
            # so we have to get sections differently.
            # Looking at you, A1M
            table_layout = False
            if len(sections) == 0:
                table_layout = True
            for section in sections:
                h3 = section.find('h3')
                section_title = h3.text.strip() if h3 else ''
                job_type = map_section_to_practice(section_title)
                job_cards = section.find_all('div', class_="opening")

                for card in job_cards:
                    a_tag = card.find("a")
                    if a_tag:
                        # Remove extraneous child elements (like 'new', 'tag', etc.)
                        for child in a_tag.find_all(['span', 'badge', 'em', 'strong']):
                            if any(cls in child.get('class', []) for cls in ['tag', 'badge', 'new', 'featured']):
                                child.decompose()
                        job_title = a_tag.get_text(strip=True)
                    else:
                        job_title = ''
                    title = f"{job_title}"
                    link = root_url + a_tag['href'] if a_tag else ''
                    loc_el = card.find('span', class_="location")
                    location = loc_el.text.strip() if loc_el else ''
                    new_job = {
                        'company': co_name,
                        'job_id': link.rsplit('/')[-1],
                        'title': title,
                        'link': link,
                        'location': location,
                        'pub_date': datetime.date.today(),
                        'job_type': job_type
                    }
                    if not already_in_jobs(new_job, jobs):
                        jobs.append(new_job)
            if table_layout: # do it again for the weird layout
                sections = soup.find_all('div', class_="job-posts")
                for section in sections:
                    h3 = section.find('h3')
                    section_title = h3.text.strip() if h3 else ''
                    job_type = map_section_to_practice(section_title)
                    job_cards = section.find_all('tr', class_="job-post")

                    for card in job_cards:
                        title_el = card.find("p", class_="body--medium")
                        if not title_el:
                            continue
                        # quick cleanup to remove junk from within th title
                        for child in title_el.find_all("span"):
                            child.decompose()
                        job_title = title_el.text.strip()
                        title = f"{job_title}"
                        a = card.find('a')
                        if not a or not a.get('href'):
                            continue
                        link = a['href']
                        meta = card.find('p', class_="body--metadata")
                        location = meta.text.strip() if meta else ''
                        new_job = {
                            'company': co_name,
                            'job_id': link.rsplit('/')[-1],
                            'title': title,
                            'link': link,
                            'location': location,
                            'pub_date': datetime.date.today(),
                            'job_type': job_type
                        }
                        if not already_in_jobs(new_job, jobs):
                            jobs.append(new_job)
        except Exception:
            continue

    return jobs

 