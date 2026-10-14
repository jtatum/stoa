package herdclient

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestRejectsRedirects(t *testing.T) {
	for _, status := range []int{301, 302, 303, 307, 308} {
		for _, crossOrigin := range []bool{false, true} {
			for _, method := range []string{"post", "participating"} {
				t.Run(fmt.Sprintf("%d/cross_origin=%t/%s", status, crossOrigin, method), func(t *testing.T) {
					var redirected, initial atomic.Int64
					targetHandler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
						redirected.Add(1)
						if method == "post" {
							fmt.Fprint(w, `{"id":1}`)
						} else {
							fmt.Fprint(w, `[]`)
						}
					})
					target := httptest.NewServer(targetHandler)
					defer target.Close()
					location := "/redirected"
					if crossOrigin {
						location = target.URL + location
					}
					source := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
						if r.URL.Path == "/redirected" {
							targetHandler.ServeHTTP(w, r)
							return
						}
						initial.Add(1)
						if got := r.Header.Get("X-API-Key"); got != "test-key" {
							t.Errorf("initial API key = %q, want test-key", got)
						}
						http.Redirect(w, r, location, status)
					}))
					defer source.Close()

					client := New("test-key").WithBaseURL(source.URL).WithTimeout(time.Second)
					var err error
					if method == "post" {
						_, err = client.GetPost(context.Background(), 1)
					} else {
						_, err = client.GetParticipating(context.Background(), time.Time{})
					}
					if got := redirected.Load(); got != 0 {
						t.Errorf("redirect target received %d requests, want 0", got)
					}
					if got := initial.Load(); got != 1 {
						t.Errorf("initial endpoint received %d requests, want 1", got)
					}
					if err == nil || !strings.Contains(err.Error(), fmt.Sprintf("HTTP %d", status)) {
						t.Fatalf("error = %v, want HTTP %d", err, status)
					}
				})
			}
		}
	}
}

func TestDirectRequests(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-API-Key") != "test-key" {
			t.Error("missing API key")
		}
		switch r.URL.Path {
		case "/api/posts/1":
			fmt.Fprint(w, `{"id":1}`)
		case "/api/posts/participating":
			fmt.Fprint(w, `[{"thread_id":1}]`)
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	client := New("test-key").WithBaseURL(server.URL)
	post, err := client.GetPost(context.Background(), 1)
	if err != nil || post.ID != 1 {
		t.Fatalf("GetPost = %v, %v", post, err)
	}
	threads, err := client.GetParticipating(context.Background(), time.Time{})
	if err != nil || len(threads) != 1 || threads[0].ThreadID != 1 {
		t.Fatalf("GetParticipating = %v, %v", threads, err)
	}
}
