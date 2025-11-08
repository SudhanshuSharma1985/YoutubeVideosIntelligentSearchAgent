import streamlit as st
import requests
from datetime import datetime, timedelta
import pandas as pd
import time
from typing import List, Dict
import math

# Configure Streamlit page
st.set_page_config(
    page_title="YouTube Video Intelligence Agent",
    page_icon="🎥",
    layout="wide"
)

class YouTubeAgent:
    """Intelligent agent to scrape and analyze YouTube video metadata using Google Cloud API"""
    
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://www.googleapis.com/youtube/v3"
        self.videos_cache = []
    
    def search_videos(self, query, max_results=50, order='relevance'):
        """
        Search for videos with specified ordering
        order options: 'relevance', 'date', 'viewCount', 'rating'
        """
        all_videos = []
        next_page_token = None
        
        try:
            # Calculate how many API calls needed (max 50 per call)
            calls_needed = math.ceil(max_results / 50)
            per_call = min(50, max_results)
            
            for call in range(calls_needed):
                search_url = f"{self.base_url}/search"
                params = {
                    'part': 'snippet',
                    'q': query,
                    'type': 'video',
                    'maxResults': per_call,
                    'order': order,
                    'key': self.api_key,
                    'regionCode': 'US',
                    'relevanceLanguage': 'en',
                    'videoDuration': 'any',
                }
                
                if next_page_token:
                    params['pageToken'] = next_page_token
                
                response = requests.get(search_url, params=params, timeout=10)
                
                if response.status_code == 403:
                    error_data = response.json()
                    if 'quotaExceeded' in str(error_data):
                        st.error("⚠️ API quota exceeded. Please try again tomorrow or use a different API key.")
                    else:
                        st.error(f"⚠️ API Error: {error_data.get('error', {}).get('message', 'Access forbidden')}")
                    return []
                
                response.raise_for_status()
                data = response.json()
                
                video_ids = [item['id']['videoId'] for item in data.get('items', [])]
                all_videos.extend(video_ids)
                
                next_page_token = data.get('nextPageToken')
                
                # Break if we got enough or no more pages
                if len(all_videos) >= max_results or not next_page_token:
                    break
                
                # Small delay to respect rate limits
                time.sleep(0.2)
            
            return all_videos[:max_results]
        
        except requests.exceptions.Timeout:
            st.error("⏱️ Request timed out. Please try again.")
            return []
        except requests.exceptions.RequestException as e:
            st.error(f"❌ Network error: {str(e)}")
            return []
        except Exception as e:
            st.error(f"❌ Error searching videos: {str(e)}")
            return []
    
    def get_video_details(self, video_ids: List[str]) -> List[Dict]:
        """Get detailed statistics for videos"""
        all_videos = []
        
        try:
            # Process in batches of 50 (API limit)
            for i in range(0, len(video_ids), 50):
                batch = video_ids[i:i+50]
                
                video_url = f"{self.base_url}/videos"
                params = {
                    'part': 'snippet,statistics,contentDetails',
                    'id': ','.join(batch),
                    'key': self.api_key
                }
                
                response = requests.get(video_url, params=params, timeout=10)
                response.raise_for_status()
                data = response.json()
                
                for item in data.get('items', []):
                    video_info = {
                        'video_id': item['id'],
                        'title': item['snippet']['title'],
                        'channel': item['snippet']['channelTitle'],
                        'channel_id': item['snippet']['channelId'],
                        'published_at': item['snippet']['publishedAt'],
                        'views': int(item['statistics'].get('viewCount', 0)),
                        'likes': int(item['statistics'].get('likeCount', 0)),
                        'comments': int(item['statistics'].get('commentCount', 0)),
                        'duration': item['contentDetails']['duration'],
                        'thumbnail': item['snippet']['thumbnails']['high']['url'],
                        'description': item['snippet'].get('description', '')[:300],
                        'tags': item['snippet'].get('tags', []),
                        'category_id': item['snippet'].get('categoryId', '')
                    }
                    all_videos.append(video_info)
                
                # Small delay between batches
                if i + 50 < len(video_ids):
                    time.sleep(0.2)
            
            return all_videos
        
        except Exception as e:
            st.error(f"❌ Error fetching video details: {str(e)}")
            return []
    
    def parse_duration(self, duration):
        """Parse ISO 8601 duration to seconds and readable format"""
        import re
        match = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration)
        if not match:
            return 0, "0:00"
        
        hours, minutes, seconds = match.groups()
        hours = int(hours) if hours else 0
        minutes = int(minutes) if minutes else 0
        seconds = int(seconds) if seconds else 0
        
        total_seconds = hours * 3600 + minutes * 60 + seconds
        
        if hours > 0:
            formatted = f"{hours}:{minutes:02d}:{seconds:02d}"
        else:
            formatted = f"{minutes}:{seconds:02d}"
        
        return total_seconds, formatted
    
    def calculate_topic_relevance(self, video, search_query):
        """
        Calculate how relevant the video is to the search topic
        Checks title, description, and tags for query keywords
        """
        query_keywords = search_query.lower().split()
        
        title = video['title'].lower()
        description = video['description'].lower()
        tags = ' '.join(video['tags']).lower() if video['tags'] else ''
        
        relevance_score = 0
        
        # Check each keyword
        for keyword in query_keywords:
            # Title matches (highest weight)
            if keyword in title:
                relevance_score += 10
                # Bonus if keyword is in first 3 words
                if keyword in ' '.join(title.split()[:3]):
                    relevance_score += 5
            
            # Tags match (medium weight)
            if keyword in tags:
                relevance_score += 5
            
            # Description match (lower weight)
            if keyword in description:
                relevance_score += 2
        
        # Bonus for exact phrase match in title
        if search_query.lower() in title:
            relevance_score += 20
        
        # Cap at 100
        return min(relevance_score, 100)
    
    def classify_difficulty_level(self, video):
        """
        Classify video difficulty level: Beginner, Intermediate, or Advanced
        Returns dict with level, score (0-100), confidence, and signals
        """
        score = 0
        signals = []
        
        title = video['title'].lower()
        description = video['description'].lower()
        tags = ' '.join(video.get('tags', [])).lower()
        duration_seconds = video.get('duration_seconds', 0)
        
        # 1. EXPLICIT KEYWORDS (40 points possible)
        beginner_keywords = [
            'beginner', 'introduction', 'intro to', 'basics', 'getting started',
            'tutorial', '101', 'fundamentals', 'first', 'start', 'learn',
            'explained', 'crash course', 'what is', 'how to start',
            'for beginners', 'complete guide', 'step by step', 'from scratch'
        ]
        
        intermediate_keywords = [
            'intermediate', 'building', 'practical', 'project',
            'guide', 'implementing', 'working with', 'hands-on',
            'deep dive', 'real-world', 'workshop'
        ]
        
        advanced_keywords = [
            'advanced', 'expert', 'optimization', 'architecture',
            'internals', 'performance', 'scalability', 'production',
            'mastering', 'under the hood', 'system design', 'best practices',
            'enterprise', 'at scale', 'professional'
        ]
        
        # Check title (highest weight - 20 points)
        title_beginner = sum(1 for kw in beginner_keywords if kw in title)
        title_intermediate = sum(1 for kw in intermediate_keywords if kw in title)
        title_advanced = sum(1 for kw in advanced_keywords if kw in title)
        
        if title_beginner > 0:
            score -= 20
            signals.append(f"Beginner keywords in title ({title_beginner})")
        if title_intermediate > 0:
            score += 0
            signals.append(f"Intermediate keywords in title ({title_intermediate})")
        if title_advanced > 0:
            score += 20
            signals.append(f"Advanced keywords in title ({title_advanced})")
        
        # Check description (10 points)
        desc_beginner = sum(1 for kw in beginner_keywords if kw in description)
        desc_advanced = sum(1 for kw in advanced_keywords if kw in description)
        
        if desc_beginner > 2:
            score -= 10
            signals.append("Multiple beginner keywords in description")
        elif desc_advanced > 2:
            score += 10
            signals.append("Multiple advanced keywords in description")
        
        # Check tags (5 points)
        if any(kw in tags for kw in beginner_keywords):
            score -= 5
            signals.append("Beginner tags present")
        elif any(kw in tags for kw in advanced_keywords):
            score += 5
            signals.append("Advanced tags present")
        
        # 2. DURATION PATTERN (15 points possible)
        if duration_seconds > 0:
            if duration_seconds < 600:  # Less than 10 min
                score -= 10
                signals.append("Short duration (quick intro/overview)")
            elif duration_seconds < 1800:  # 10-30 min
                score += 0
                signals.append("Medium duration (tutorial)")
            elif duration_seconds < 3600:  # 30-60 min
                score += 8
                signals.append("Long duration (detailed/in-depth)")
            else:  # 60+ min
                score += 12
                signals.append("Very long (comprehensive/masterclass)")
        
        # 3. TECHNICAL DENSITY (15 points possible)
        technical_terms = [
            'api', 'framework', 'algorithm', 'optimization', 'async',
            'architecture', 'pattern', 'dependency', 'configuration',
            'deployment', 'testing', 'ci/cd', 'kubernetes', 'docker',
            'microservices', 'database', 'query', 'cache', 'authentication'
        ]
        
        tech_count = sum(1 for term in technical_terms 
                        if term in description or term in title or term in tags)
        
        if tech_count == 0:
            score -= 10
            signals.append("Low technical density")
        elif tech_count <= 3:
            score += 0
            signals.append(f"Moderate technical density ({tech_count} terms)")
        else:
            score += 10
            signals.append(f"High technical density ({tech_count} terms)")
        
        # 4. PREREQUISITE INDICATORS (15 points possible)
        prerequisite_phrases = [
            'should know', 'familiarity with', 'assumes you know',
            'prior knowledge', 'experience with', 'already know',
            'prerequisite', 'requires understanding'
        ]
        
        no_prerequisite_phrases = [
            'no experience', 'no prior knowledge', 'complete beginner',
            'never coded', 'from scratch', 'zero to hero', 'no prerequisites'
        ]
        
        has_prerequisites = any(phrase in description for phrase in prerequisite_phrases)
        no_prerequisites = any(phrase in description for phrase in no_prerequisite_phrases)
        
        if no_prerequisites:
            score -= 15
            signals.append("Explicitly states no prerequisites needed")
        elif has_prerequisites:
            score += 15
            signals.append("Prerequisites mentioned")
        
        # 5. CONTENT TYPE INDICATORS (10 points possible)
        project_words = ['project', 'build', 'create', 'make', 'develop']
        theory_words = ['theory', 'concept', 'understanding', 'explain', 'why']
        
        if any(word in title.lower() for word in project_words):
            score += 5
            signals.append("Project-based (typically intermediate+)")
        
        if sum(1 for word in theory_words if word in title.lower()) >= 2:
            score -= 5
            signals.append("Theory-focused (often beginner-friendly)")
        
        # 6. ENGAGEMENT QUALITY (5 points bonus)
        engagement = video.get('engagement_score', 0)
        if engagement > 5:
            # High engagement on what appears to be beginner content
            if score < -10:
                score -= 5
                signals.append("High engagement (well-explained beginner content)")
        
        # Normalize score to 0-100 range
        # Current range is roughly -65 to +85, center at 0
        normalized_score = max(0, min(100, ((score + 65) / 150) * 100))
        
        # Classify based on normalized score
        if normalized_score < 33:
            level = 'Beginner'
            emoji = '🟢'
            confidence = "High" if normalized_score < 20 else "Medium"
        elif normalized_score < 67:
            level = 'Intermediate'
            emoji = '🟡'
            confidence = "Medium"
        else:
            level = 'Advanced'
            emoji = '🔴'
            confidence = "High" if normalized_score > 80 else "Medium"
        
        return {
            'level': level,
            'emoji': emoji,
            'score': round(normalized_score, 1),
            'confidence': confidence,
            'signals': signals
        }
    
    def calculate_metrics(self, videos, search_query=""):
        """Calculate additional engagement and popularity metrics"""
        for video in videos:
            # Parse duration
            duration_sec, duration_formatted = self.parse_duration(video['duration'])
            video['duration_seconds'] = duration_sec
            video['duration_formatted'] = duration_formatted
            
            # Calculate engagement metrics
            views = video['views']
            if views > 0:
                video['like_ratio'] = round((video['likes'] / views) * 100, 2)
                video['comment_ratio'] = round((video['comments'] / views) * 100, 2)
                video['engagement_score'] = round(
                    (video['likes'] * 0.6 + video['comments'] * 0.4) / views * 100, 2
                )
            else:
                video['like_ratio'] = 0
                video['comment_ratio'] = 0
                video['engagement_score'] = 0
            
            # Calculate topic relevance
            video['topic_relevance'] = self.calculate_topic_relevance(video, search_query)
            
            # Classify difficulty level
            difficulty_data = self.classify_difficulty_level(video)
            video['difficulty_level'] = difficulty_data['level']
            video['difficulty_emoji'] = difficulty_data['emoji']
            video['difficulty_score'] = difficulty_data['score']
            video['difficulty_confidence'] = difficulty_data['confidence']
            video['difficulty_signals'] = difficulty_data['signals']
            
            # Calculate popularity score (weighted combination)
            # Normalize views (log scale to handle huge variance)
            view_score = math.log10(views + 1) * 10
            like_score = video['like_ratio'] * 5
            engagement = video['engagement_score'] * 3
            comment_score = video['comment_ratio'] * 2
            
            video['popularity_score'] = round(
                view_score + like_score + engagement + comment_score, 2
            )
            
            # Calculate days since published
            published_date = datetime.fromisoformat(video['published_at'].replace('Z', '+00:00'))
            days_old = (datetime.now(published_date.tzinfo) - published_date).days
            video['days_old'] = days_old
            
            # Freshness score (newer videos get boost)
            if days_old < 7:
                freshness_multiplier = 1.3
            elif days_old < 30:
                freshness_multiplier = 1.15
            elif days_old < 90:
                freshness_multiplier = 1.0
            else:
                freshness_multiplier = 0.9
            
            # RELEVANCE SCORE: Prioritize views (50%), then topic relevance (30%), then engagement (20%)
            # Normalize views to 0-100 scale using log
            normalized_views = min(math.log10(views + 1) * 10, 100)
            
            video['relevance_score'] = round(
                (normalized_views * 0.50) +           # 50% weight to views
                (video['topic_relevance'] * 0.30) +   # 30% weight to topic match
                (video['engagement_score'] * 0.20) * freshness_multiplier,  # 20% to engagement with freshness
                2
            )
        
        return videos

def format_number(num):
    """Format large numbers for display"""
    if num >= 1_000_000_000:
        return f"{num/1_000_000_000:.1f}B"
    elif num >= 1_000_000:
        return f"{num/1_000_000:.1f}M"
    elif num >= 1_000:
        return f"{num/1_000:.1f}K"
    else:
        return str(num)

def format_date(date_str):
    """Format ISO date to readable format"""
    try:
        date_obj = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        return date_obj.strftime('%b %d, %Y')
    except:
        return date_str

def main():
    st.title("🤖 YouTube Video Intelligence Agent")
    st.markdown("AI-powered video discovery with smart relevance & difficulty analysis")
    
    # Sidebar configuration
    with st.sidebar:
        st.header("⚙️ Agent Configuration")
        
        # API Key input
        api_key = st.text_input(
            "🔑 YouTube API Key",
            type="password",
            help="Enter your Google Cloud YouTube Data API v3 key"
        )
        
        if api_key:
            st.success("✅ API Key loaded")
        
        st.markdown("---")
        
        # Search parameters
        st.subheader("🔍 Search Parameters")
        
        search_query = st.text_input(
            "Search Topic",
            value="langchain tutorial",
            placeholder="e.g., autogen, AI agents, python"
        )
        
        max_results = st.slider(
            "Number of Videos",
            min_value=10,
            max_value=100,
            value=50,
            step=10,
            help="More videos = more API quota used"
        )
        
        search_order = st.selectbox(
            "Initial Search Order",
            ["relevance", "viewCount", "date", "rating"],
            help="How YouTube initially finds videos"
        )
        
        st.markdown("---")
        
        # Sorting parameters
        st.subheader("📊 Sort & Filter")
        
        sort_by = st.selectbox(
            "Sort Results By",
            [
                "Relevance Score (AI)",
                "Popularity Score",
                "Views",
                "Engagement Score",
                "Difficulty Level",
                "Likes",
                "Like Ratio %",
                "Comments",
                "Published Date",
                "Duration"
            ]
        )
        
        # Difficulty filter - PROMINENTLY DISPLAYED
        st.markdown("### 🎓 Filter by Difficulty Level")
        difficulty_filter = st.multiselect(
            "Select Difficulty Levels to Show",
            options=["Beginner", "Intermediate", "Advanced"],
            default=["Beginner", "Intermediate", "Advanced"],
            help="Filter videos by their difficulty classification"
        )
        
        st.markdown("---")
        st.subheader("🔧 Additional Filters")
        
        # Filters
        min_views = st.number_input(
            "Min Views",
            min_value=0,
            value=0,
            step=1000,
            format="%d"
        )
        
        min_duration = st.number_input(
            "Min Duration (seconds)",
            min_value=0,
            value=0,
            step=60,
            format="%d"
        )
        
        max_days_old = st.number_input(
            "Max Age (days)",
            min_value=0,
            value=0,
            step=7,
            help="0 = no limit",
            format="%d"
        )
        
        search_button = st.button(
            "🚀 Analyze Videos",
            type="primary",
            use_container_width=True
        )
        
        st.markdown("---")
        
        # Help section
        with st.expander("📖 How to Get API Key"):
            st.markdown("""
            1. Go to [Google Cloud Console](https://console.cloud.google.com)
            2. Create a new project
            3. Enable **YouTube Data API v3**
            4. Go to Credentials → Create API Key
            5. Copy and paste here
            
            **Free Quota:** 10,000 units/day
            - Search: 100 units
            - Video details: 1 unit per video
            """)
    
    # Main content
    if not api_key:
        st.info("👈 Please enter your YouTube API Key in the sidebar to start")
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.markdown("""
            ### 🎯 Features
            - **Smart Relevance Scoring** - AI-powered video ranking
            - **Difficulty Classification** - Auto-detect Beginner/Intermediate/Advanced
            - **Popularity Analysis** - Multi-factor engagement metrics
            - **Advanced Filtering** - Views, duration, age, difficulty filters
            - **Detailed Metadata** - Views, likes, comments, ratios
            - **Export to CSV** - Download all analysis data
            """)
        
        with col2:
            st.markdown("""
            ### 📊 Metrics Calculated
            - **Relevance Score** - 50% Views + 30% Topic Match + 20% Engagement
            - **Difficulty Level** - Multi-factor AI classification
            - **Topic Relevance** - Keyword match in title, tags, description
            - **Popularity Score** - Weighted views + engagement
            - **Engagement Score** - Likes + comments / views
            - **Like Ratio** - Percentage of viewers who liked
            """)
        
        return
    
    # Search execution
    if search_button or 'videos_data' in st.session_state:
        if search_button:
            with st.spinner(f"🔍 Analyzing videos for '{search_query}'..."):
                agent = YouTubeAgent(api_key)
                
                # Search for videos
                video_ids = agent.search_videos(search_query, max_results, search_order)
                
                if not video_ids:
                    st.warning("No videos found or API error occurred.")
                    return
                
                st.info(f"📥 Found {len(video_ids)} videos, fetching details...")
                
                # Get detailed information
                videos_data = agent.get_video_details(video_ids)
                
                if not videos_data:
                    st.warning("Could not fetch video details.")
                    return
                
                # Calculate all metrics
                videos_data = agent.calculate_metrics(videos_data, search_query)
                
                st.session_state.videos_data = videos_data
                st.session_state.search_query = search_query
                st.success(f"✅ Analysis complete! Found {len(videos_data)} videos")
        
        videos_data = st.session_state.videos_data
        search_query = st.session_state.search_query
        
        # Apply filters
        filtered_videos = videos_data.copy()
        
        # Difficulty filter
        if difficulty_filter:
            filtered_videos = [v for v in filtered_videos if v['difficulty_level'] in difficulty_filter]
        
        if min_views > 0:
            filtered_videos = [v for v in filtered_videos if v['views'] >= min_views]
        
        if min_duration > 0:
            filtered_videos = [v for v in filtered_videos if v['duration_seconds'] >= min_duration]
        
        if max_days_old > 0:
            filtered_videos = [v for v in filtered_videos if v['days_old'] <= max_days_old]
        
        # Sort videos
        sort_key_map = {
            "Relevance Score (AI)": "relevance_score",
            "Popularity Score": "popularity_score",
            "Views": "views",
            "Engagement Score": "engagement_score",
            "Difficulty Level": "difficulty_score",
            "Likes": "likes",
            "Like Ratio %": "like_ratio",
            "Comments": "comments",
            "Published Date": "days_old",
            "Duration": "duration_seconds"
        }
        
        reverse_sort = sort_by != "Published Date"
        sorted_videos = sorted(
            filtered_videos,
            key=lambda x: x[sort_key_map[sort_by]],
            reverse=reverse_sort
        )
        
        # Display summary
        st.header(f"📊 Analysis Results: {search_query}")
        
        col1, col2, col3, col4, col5 = st.columns(5)
        
        total_views = sum(v['views'] for v in sorted_videos)
        total_likes = sum(v['likes'] for v in sorted_videos)
        total_comments = sum(v['comments'] for v in sorted_videos)
        avg_engagement = sum(v['engagement_score'] for v in sorted_videos) / len(sorted_videos) if sorted_videos else 0
        avg_relevance = sum(v['relevance_score'] for v in sorted_videos) / len(sorted_videos) if sorted_videos else 0
        
        col1.metric("📹 Videos", len(sorted_videos))
        col2.metric("👁️ Total Views", format_number(total_views))
        col3.metric("👍 Total Likes", format_number(total_likes))
        col4.metric("🎯 Avg Engagement", f"{avg_engagement:.1f}%")
        col5.metric("⭐ Avg Relevance", f"{avg_relevance:.0f}")
        
        # Difficulty distribution
        st.markdown("---")
        st.subheader("📚 Difficulty Distribution")
        diff_col1, diff_col2, diff_col3 = st.columns(3)
        
        beginner_count = len([v for v in sorted_videos if v['difficulty_level'] == 'Beginner'])
        intermediate_count = len([v for v in sorted_videos if v['difficulty_level'] == 'Intermediate'])
        advanced_count = len([v for v in sorted_videos if v['difficulty_level'] == 'Advanced'])
        
        diff_col1.metric("🟢 Beginner", beginner_count)
        diff_col2.metric("🟡 Intermediate", intermediate_count)
        diff_col3.metric("🔴 Advanced", advanced_count)
        
        st.markdown("---")
        
        # Display videos
        st.subheader(f"🎬 Top Videos (Sorted by {sort_by})")
        
        for idx, video in enumerate(sorted_videos[:50], 1):  # Show top 50
            with st.container():
                col1, col2 = st.columns([1, 2])
                
                with col1:
                    st.image(video['thumbnail'], use_container_width=True)
                    
                    # Score badges - added difficulty here too
                    badge_col1, badge_col2, badge_col3 = st.columns(3)
                    badge_col1.metric("⭐ Relevance", f"{video['relevance_score']:.0f}")
                    badge_col2.metric("🔥 Popularity", f"{video['popularity_score']:.0f}")
                    badge_col3.metric(f"{video.get('difficulty_emoji', '🟢')}", video.get('difficulty_level', 'N/A'))
                
                with col2:
                    # Title
                    st.markdown(f"### {idx}. {video['title']}")
                    
                    # Difficulty badge prominently displayed
                    difficulty_emoji = video.get('difficulty_emoji', '🟢')
                    difficulty_level = video.get('difficulty_level', 'Unknown')
                    difficulty_conf = video.get('difficulty_confidence', 'Low')
                    st.markdown(f"### {difficulty_emoji} **{difficulty_level}** ({difficulty_conf} confidence)")
                    
                    # Channel and date info
                    st.markdown(f"**📺 {video['channel']}** • {format_date(video['published_at'])} ({video['days_old']} days ago)")
                    
                    # Relevance breakdown
                    st.caption(f"🎯 Topic Match: {video['topic_relevance']}/100")
                    
                    # Main metrics
                    m1, m2, m3, m4, m5 = st.columns(5)
                    m1.metric("👁️ Views", format_number(video['views']))
                    m2.metric("👍 Likes", format_number(video['likes']))
                    m3.metric("💬 Comments", format_number(video['comments']))
                    m4.metric("❤️ Like %", f"{video['like_ratio']}%")
                    m5.metric("🎯 Engage", f"{video['engagement_score']}%")
                    
                    # Duration
                    st.caption(f"⏱️ Duration: {video['duration_formatted']}")
                    
                    # Description and difficulty signals
                    with st.expander("📝 Details & Classification"):
                        st.write("**Description:**")
                        st.write(video['description'])
                        if video.get('tags'):
                            st.write(f"**Tags:** {', '.join(video['tags'][:10])}")
                        
                        st.write(f"\n**🎓 Difficulty Classification Signals:**")
                        st.write(f"Difficulty Score: {video.get('difficulty_score', 0):.1f}/100")
                        if video.get('difficulty_signals'):
                            for signal in video['difficulty_signals']:
                                st.write(f"• {signal}")
                    
                    # Watch link
                    st.markdown(f"[🔗 Watch on YouTube](https://www.youtube.com/watch?v={video['video_id']})")
                
                st.markdown("---")
        
        # Data export
        st.subheader("📥 Export Data")
        
        # Prepare DataFrame
        export_data = []
        for video in sorted_videos:
            export_data.append({
                'Rank': sorted_videos.index(video) + 1,
                'Title': video['title'],
                'Channel': video['channel'],
                'Difficulty_Level': video['difficulty_level'],
                'Difficulty_Confidence': video['difficulty_confidence'],
                'URL': f"https://www.youtube.com/watch?v={video['video_id']}",
                'Views': video['views'],
                'Likes': video['likes'],
                'Comments': video['comments'],
                'Like_Ratio_%': video['like_ratio'],
                'Engagement_Score_%': video['engagement_score'],
                'Topic_Relevance': video['topic_relevance'],
                'Relevance_Score': video['relevance_score'],
                'Popularity_Score': video['popularity_score'],
                'Duration': video['duration_formatted'],
                'Published': format_date(video['published_at']),
                'Days_Old': video['days_old']
            })
        
        df = pd.DataFrame(export_data)
        
        col1, col2 = st.columns(2)
        
        with col1:
            csv = df.to_csv(index=False)
            st.download_button(
                label="📄 Download CSV",
                data=csv,
                file_name=f"youtube_analysis_{search_query.replace(' ', '_')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv",
                use_container_width=True
            )
        
        with col2:
            if st.button("📊 Show Data Table", use_container_width=True):
                st.dataframe(df, use_container_width=True, height=400)

if __name__ == "__main__":
    main()
