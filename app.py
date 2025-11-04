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
    st.markdown("AI-powered video discovery with smart relevance & popularity analysis")
    
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
                "Likes",
                "Like Ratio %",
                "Comments",
                "Published Date",
                "Duration"
            ]
        )
        
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
            - **Popularity Analysis** - Multi-factor engagement metrics
            - **Advanced Filtering** - Views, duration, age filters
            - **Detailed Metadata** - Views, likes, comments, ratios
            - **Export to CSV** - Download all analysis data
            """)
        
        with col2:
            st.markdown("""
            ### 📊 Metrics Calculated
            - **Relevance Score** - 50% Views + 30% Topic Match + 20% Engagement
            - **Topic Relevance** - Keyword match in title, tags, description
            - **Popularity Score** - Weighted views + engagement
            - **Engagement Score** - Likes + comments / views
            - **Like Ratio** - Percentage of viewers who liked
            - **Comment Ratio** - Viewer interaction rate
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
        
        st.markdown("---")
        
        # Display videos
        st.subheader(f"🎬 Top Videos (Sorted by {sort_by})")
        
        for idx, video in enumerate(sorted_videos[:50], 1):  # Show top 50
            with st.container():
                col1, col2 = st.columns([1, 2])
                
                with col1:
                    st.image(video['thumbnail'], use_container_width=True)
                    
                    # Score badges
                    badge_col1, badge_col2 = st.columns(2)
                    badge_col1.metric("⭐ Relevance", f"{video['relevance_score']:.0f}")
                    badge_col2.metric("🔥 Popularity", f"{video['popularity_score']:.0f}")
                
                with col2:
                    # Title and channel
                    st.markdown(f"### {idx}. {video['title']}")
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
                    
                    # Description
                    with st.expander("📝 Description"):
                        st.write(video['description'])
                        if video['tags']:
                            st.write(f"**Tags:** {', '.join(video['tags'][:10])}")
                    
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