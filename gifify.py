import PIL.GifImagePlugin
try : import ujson as json
except : import json
import subprocess
import traceback
import requests
import colorama
import twitter
import time
import sys
import os


from requests_html import HTMLSession
from urllib.parse import urlparse, quote
from re import compile as re_compile
import logging
from hashlib import md5
from jmespath import compile as jmespath_compile
from shutil import copyfileobj, rmtree
from PIL.GifImagePlugin import GifImageFile
from collections import defaultdict
import asyncio


logger = logging.getLogger('gifify')


class QuitParsing(Exception) :
	pass


def secondsFromTimecode(timecode) :
	seconds = 0
	conversions = [86400, 3600, 60, 1]
	timeslices = timecode.split(':')
	while timeslices :
		seconds += conversions.pop() * float(timeslices.pop())
	return seconds


class Gifify :

	counter = 0
	acceptedTypes = {
		# mime type : file extension
		'video/webm': 'webm',
		'video/mp4': 'mp4',
		'image/gif': 'gif',
		'video/quicktime': 'mov',
		'application/x-shockwave-flash': 'swf',
	}
	subcommands = {
		'length': secondsFromTimecode,
		'start': secondsFromTimecode,
		'end': secondsFromTimecode,
	}
	basicCommands = defaultdict(lambda : "Sorry, I didn't understand that. Try running /help to see what this bot can do.", {
		'/start': 'I can convert your favorite media into gifs! Either send me a link in this chat or add me to a group and use /gifify!',
		'/help': 'I can convert your favorite media into gifs! Either send me a link in this chat or add me to a group and use /gifify!',
	})
	maxretries = 10

	statusRegex = re_compile(r'\/status\/(\d+)')
	urljmespath = jmespath_compile(r"entities[?type=='url'] | [0]")
	commandsjmespath = jmespath_compile(r"entities[?type=='bot_command'] | [0]")
	twitterjmespath = jmespath_compile(r"media[?type=='video' || type=='animated_gif'].video_info.max_by(variants[?bitrate], &bitrate) | [0].url")
	mediaregex = re_compile(r'''"((?:https?:\/\/)?[\w\/\.\_\-]+\.(''' + '|'.join(acceptedTypes.values()) + r''')(?:\?.*?)?)"''')
	chatjmespath = jmespath_compile(r"message.chat.id")

	widthjmespath = jmespath_compile(r"max(streams[?codec_type=='video'].width)")
	heightjmespath = jmespath_compile(r"max(streams[?codec_type=='video'].height)")
	lengthjmespath = jmespath_compile(r"max(streams[].duration)")
	maxbitratejmespath = jmespath_compile(r"max(streams[?codec_type=='video'].max_bit_rate)")
	bitratejmespath = jmespath_compile(r"max(streams[?codec_type=='video'].bit_rate)")

	framelimit = 1280
	filelimit = 8000


	def __init__(self) :
		print('loading credentials...', end='', flush=True)
		with open('credentials.json') as credentials :
			credentials = json.load(credentials)
			self._telegram_access_token = credentials['telegram_access_token']
			self._telegram_bot_id = credentials['telegram_bot_id']
			try :
				self._twitter_api = twitter.Api(
					consumer_key = credentials['twitter']['consumer_key'],
					consumer_secret = credentials['twitter']['consumer_secret'],
					access_token_key = credentials['twitter']['access_token_key'],
					access_token_secret = credentials['twitter']['access_token_secret'],
					tweet_mode='extended',
				)
			except :
				print(' (failed to initialize twitter)... ', end='', flush=True)
		print('success.')


	def generateId(self) :
		self.counter += 1
		return str(md5(str(self.counter + time.time()).encode()).hexdigest()) 


	def downloadFileForConversion(self, url=None, response=None, extension=None) :
		if not response : response = requests.get(url, stream=True, timeout=300)
		folder = self.generateId()
		name = f'{self.generateId()}.{extension or self.acceptedTypes[response.headers["content-type"]]}'
		os.mkdir(folder)
		with open(f'{folder}/{name}', 'wb') as file :
			copyfileobj(response.raw, file)
		return {
			'folder': folder,
			'filename': name,
			'source': url or response.url,
		}


	def downloadFromTwitter(self, url) :
		# ex: https://twitter.com/DailyHyenas/status/1279065670643302401?s=20
		status = self.statusRegex.search(url).group(1)
		status = self._twitter_api.GetStatus(status).AsDict()
		mediaurl = self.twitterjmespath.search(status)
		return { **self.downloadFileForConversion(mediaurl), 'source': url }


	def sendMessage(self, chat, message) :
		# mode = MarkdownV2 or HTML
		request = f'https://api.telegram.org/bot{self._telegram_access_token}/sendMessage?chat_id={chat}&parse_mode=HTML&text='
		url = request + message
		info = 'failed to send message to telegram.'
		for _ in range(5) :
			try :
				info = json.loads(requests.get(url).text)
				if not info['ok'] :
					break
				return True
			except :
				pass
		logger.error({
			'message': info,
			'request': request,
			'telegram message': message,
			'chat': chat,
		})
		return False


	def sendGif(self, chat, filename, source=None) :
		# mode = MarkdownV2 or HTML
		request = f'https://api.telegram.org/bot{self._telegram_access_token}/sendDocument?chat_id={chat}'

		if source :
			request += f'&caption={quote(source)}'

		with open(filename, 'rb') as gif :
			postdata = { 'document': gif }
			for _ in range(5) :
				try :
					info = json.loads(requests.post(request, files=postdata, timeout=300))
					if not info['ok'] :
						break
					return True
				except :
					pass
		return False


	def downloadDocument(self, document) :
		# returns path to file downloaded or errors (folder, name)
		mime = document.get('mime_type')
		if mime in self.acceptedTypes :
			response = json.loads(requests.get(f'https://api.telegram.org/bot{self._telegram_access_token}/getFile?file_id={document["file_id"]}', stream=True, timeout=30).text)
			return { **self.downloadFileForConversion(f'https://api.telegram.org/file/bot{self._telegram_access_token}/{response["result"]["file_path"]}', extension=self.acceptedTypes[mime]), 'source': None }
		raise ValueError('message contains an incorrect file type.')


	def parseLink(self, url) :
		domain = urlparse(url).netloc
		if 'twitter.com' in domain and '/status/' in url :
			return self.downloadFromTwitter(url)

		response = HTMLSession().get(url, stream=True, timeout=300)

		if response.headers['content-type'] in self.acceptedTypes :
			return self.downloadFileForConversion(response=response)

		else :
			# find the url
			mediaurls = { b: a for a, b in self.mediaregex.findall(response.text)}
			for filetype in self.acceptedTypes.values() :
				if filetype in mediaurls :
					mediaurl = mediaurls[filetype]
					break
			
			if not mediaurl.startswith('http') :
				if domain not in mediaurl :
					mediaurl = domain + mediaurl
				mediaurl = 'https://' + mediaurl.strip('/:')

			print(f'({mediaurl})...', flush=True, end='')

			return { **self.downloadFileForConversion(mediaurl), 'source': url }


	def retrieveMedia(self, message) :
		# returns path to file downloaded or errors

		# check the basics, look for video or document in the message json
		if 'document' in message :
			return self.downloadDocument(message['document'])
		elif 'video' in message :
			return self.downloadDocument(message['video'])

		entity = self.urljmespath.search(message)
		if entity :
			end = entity['offset'] + entity['length']
			link = message['text'][entity['offset']:end]

		else :
			raise QuitParsing(self.basicCommands[None])

		return self.parseLink(link)


	def examineFile(self, filename) :
		# returns filesize, width, height, length
		filesize = os.path.getsize(filename) / 1024  # convert to kilobytes
		if filename.endswith('.gif') :
			with PIL.GifImagePlugin.GifImageFile(fp=filename) as gif :
				length = (gif.n_frames + 1) * gif.info['duration'] / 1000 # divide by 1000 to get seconds
				return filesize, gif.width, gif.height, length

		call = ('ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams', filename)

		output = json.loads(subprocess.check_output(call).decode())
		length = self.lengthjmespath.search(output)
		length = float(length) if length else 1

		return (
			# bitrate
			# self.maxbitratejmespath.search(output) or self.bitratejmespath.search(output) or filesize / length,
			# filesize
			filesize,
			# width
			self.widthjmespath.search(output),
			# height
			self.heightjmespath.search(output),
			# length (seconds)
			length,
		)


	def convertFileToGif(self, folder, filename, **kwargs) :

		filesize, width, height, length = self.examineFile(f'{folder}/{filename}')
		misc = []

		if width > self.framelimit or height > self.framelimit :
			if width >= height :
				misc = ['-vf', 'scale=1280:-2']
			else :
				misc = ['-vf', 'scale=-2:1280']
		elif width % 2 != 0 or height % 2 != 0 :
			misc = ['-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2']

		inputoptions = []

		if 'length' in kwargs :
			length = length or kwargs['length']

		if 'end' in kwargs :
			length = kwargs['end']
			inputoptions += ['-to', str(kwargs['end'])]

		if 'start' in kwargs :
			length -= kwargs['start']
			inputoptions += ['-ss', str(kwargs['start'])]

		if 'source' in kwargs :
			source = kwargs['source']
			if source and source.endswith('.mp4') : source = source[:-4]
			gifname = f'{folder}/gif by gifify - {source}.mp4'

		# else :
		gifname = f'{folder}/gif by gifify.mp4'

		retries = 0
		finalsize = 0

		while not finalsize or finalsize > self.filelimit :
			if finalsize :
				_q *= self.filelimit / finalsize * 0.95
				quality = f'-b:v {_q}k'.split()
			elif filesize < self.filelimit * 0.95 and filename.endswith('.mp4') :
				_q = filesize * 8 / length  # get a rough bitrate incase it loops
				quality = '-c copy'.split()  # if it's an mp4 within the filelimit, just cut out the audio and copy the video directly
			else :
				_q = min(self.filelimit * 0.95 * self.filelimit * 8 / length, 8000)
				quality = f'-b:v {_q}k'.split()

			call = 'ffmpeg', *inputoptions, '-i', f'{folder}/{filename}', *quality, *misc, '-pix_fmt', 'yuv420p', '-loglevel', 'quiet', '-an', gifname, '-y'
			subprocess.call(call)

			finalsize = os.path.getsize(gifname) / 1024
			print(f'({round(finalsize, 2)}kb/{round(_q, 2)}kb/s)...', flush=True, end='')
			retries += 1
			if retries > self.maxretries :
				raise ValueError('too many retries')
		
		return gifname


	def processSubcommands(self, text) :
		if not text :
			return { }
		text = text.split()
		subcommands = { }
		for t in text :
			t = t.split('=')
			if len(t) == 2 and t[0] in self.subcommands :
				subcommands[t[0]] = self.subcommands[t[0]](t[1])
		return subcommands


	def parseMessage(self, message) :
		starttime = time.time()

		# stage 0
		print('request received...', end='', flush=True)

		user = message['from']['id']
		chat = message['chat']['id']
		command = None

		entity = self.commandsjmespath.search(message)
		if entity :
			end = entity['offset'] + entity['length']
			command = message['text'][entity['offset']:end].lower().split('@')[0]

		# exit if it's a group and the command isn't gifify
		if command != '/gifify' :
			if user != chat :
				print('done.')
				return True
			elif command :
				return self.sendMessage(chat, self.basicCommands[command])

		# stage 1
		print('downloading media...', end='', flush=True)
		data = self.retrieveMedia(message.get('reply_to_message', message))

		# stage 2
		print('converting...', end='', flush=True)
		gif = self.convertFileToGif(data['folder'], data['filename'], source=data['source'], **self.processSubcommands(message.get('text')))

		# stage 3
		print('sending gif...', end='', flush=True)
		self.sendGif(chat, gif, data['source'])

		# stage 4 TODO
		print('cleaning up...', end='', flush=True)
		rmtree(data['folder'])  # delete the folder and contents

		print('done.')


	def run(self) :

		count = 0
		for update in self.recv() :
			try :
				self.parseMessage(update['message'])

			except QuitParsing as e :
				print('done.')
				chat = self.chatjmespath.search(update)
				if chat :
					self.sendMessage(chat, str(e))

			except :
				trace = self.generateId()
				print('failed.', trace)
				chat = self.chatjmespath.search(update)
				if chat :
					self.sendMessage(chat, f"I'm sorry, it looks like I've run into an error. trace: {trace}")
				logger.exception({
					'message': 'failed to parse message.',
					'trace': trace,
					'telegram update': update,
				})

			finally :
				count += 1


	def recv(self) :
		# just let it fail if it's not json serialized
		request = f'https://api.telegram.org/bot{self._telegram_access_token}/getUpdates?offset='
		mostrecent = 0
		while True :
			try :
				updates = json.loads(requests.get(request + str(mostrecent)).text)
				if updates['ok'] and updates['result'] :
					mostrecent = updates['result'][-1]['update_id'] + 1
					yield from updates['result']
			except :
				logger.exception('failed to read updates from telegram.')

			time.sleep(1)


if __name__ == '__main__' :
	from ast import literal_eval
	kwargs = { k: literal_eval(v) for k, v in (arg.split('=') for arg in sys.argv[1:]) }
	try :
		gifify = Gifify(**kwargs)
		loop = asyncio.get_event_loop()
		# Blocking call which returns when the run() coroutine is done
		loop.run_until_complete(gifify.run())
		time.sleep(5)
		loop.close()
	finally :
		pass
